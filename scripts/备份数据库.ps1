<#
.SYNOPSIS
    备份 PostgreSQL：pg_dump 全量 + gzip，落到仓库外的备份目录并校验。

.DESCRIPTION
    此前**没有备份、没有恢复演练**（见 RISKS.md 第一节第 11、12 条）。
    本脚本补上第一条：一条命令拿到一份**可校验**的全量备份。
    第二条由 [恢复演练.ps1](恢复演练.ps1) 负责 —— 备份没恢复过等于没有备份。

    为什么这么实现（三条都是本机实测约束，不是偏好）：

    1. **宿主机没有 pg_dump / psql**，只能用容器里的（`pg_dump 16.15`）。
    2. **二进制不能过 PowerShell 管道**：实测
       `docker exec ... cat 二进制 | Set-Content -AsByteStream` 会得到 **0 字节**。
       所以改成"容器内落盘 → docker cp 出来"，全程不碰管道。
    3. **gzip 也在容器内做**（同一个原因），宿主机只负责搬文件。

    ⚠️ 容器里唯一可写的共享目录是 PostgreSQL 的数据目录（bind mount），
    往那里写是自找麻烦 —— 所以中转文件放容器的 `/tmp`（可写层，随容器销毁），
    用 `docker cp` 取出。这份备份的**生命周期与容器无关**。

.PARAMETER BackupDir
    备份落盘目录。默认 `D:\develop\procurement\backups`（与数据库数据同级，
    **在仓库之外** —— 备份不该进版本库，见 README"仓库组成"）。

.PARAMETER Keep
    保留最近多少份，超出部分按文件名（时间戳）从旧到新删。默认 14。取 0 = 不清理。

.PARAMETER Database
    备份哪个库，默认 `procurement`。

.EXAMPLE
    .\scripts\备份数据库.ps1

.EXAMPLE
    .\scripts\备份数据库.ps1 -Keep 30 -BackupDir E:\pg-backups

.NOTES
    只读操作 + 在容器 /tmp 与备份目录写文件。**不触碰数据库内容、不删除任何库表。**
    清理只删本脚本自己产出的 `procurement-*.sql.gz`。
#>
[CmdletBinding()]
param(
    [string]$BackupDir = 'D:\develop\procurement\backups',

    [ValidateRange(0, 3650)]
    [int]$Keep = 14,

    [string]$Database = 'procurement',

    [string]$Container = 'procurement-db',

    [string]$User = 'app'
)

$ErrorActionPreference = 'Stop'

# ---- 前置检查：容器必须在跑（否则后面每个 docker 命令都会各报一次同样的错）----
$state = docker inspect $Container --format '{{.State.Status}}' 2>&1
if ($LASTEXITCODE -ne 0 -or $state -notmatch 'running') {
    throw "容器 $Container 不在运行（状态：$state）。先跑 .\scripts\dev-up.ps1"
}

if (-not (Test-Path -LiteralPath $BackupDir)) {
    New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
    Write-Host "已创建备份目录：$BackupDir"
}

# ---- 时间戳取自**容器**，与数据库所在时钟一致（跨时区跑批时这一点有意义）----
$stamp = (docker exec $Container date -u '+%Y%m%dT%H%M%SZ' 2>&1).Trim()
if ($LASTEXITCODE -ne 0 -or $stamp -notmatch '^\d{8}T\d{6}Z$') {
    throw "取容器时间失败：$stamp"
}
$name = "$Database-$stamp.sql.gz"
$target = Join-Path $BackupDir $name

# 容器内中转路径。**不用 $pid —— 它是 PowerShell 的只读自动变量。**
$tmpSql = "/tmp/dsh-backup-$stamp.sql"
$tmpGz = "$tmpSql.gz"

Write-Host "备份中：$Database → $target"

# ---- 1) 容器内 pg_dump | gzip（管道发生在**容器内部**，不经过 PowerShell）----
docker exec $Container sh -c "set -e; pg_dump -U $User -d $Database | gzip -9 > $tmpGz"
if ($LASTEXITCODE -ne 0) {
    docker exec $Container rm -f $tmpGz $tmpSql 2>&1 | Out-Null
    throw "pg_dump / gzip 失败（退出码 $LASTEXITCODE）"
}

# ---- 2) 取出到宿主机 ----
docker cp "${Container}:${tmpGz}" $target
if ($LASTEXITCODE -ne 0) {
    docker exec $Container rm -f $tmpGz 2>&1 | Out-Null
    throw "docker cp 失败（退出码 $LASTEXITCODE）"
}

# ---- 3) 校验：非空 + gzip 完整性 + 里层是 SQL ----
$size = (Get-Item -LiteralPath $target).Length
if ($size -le 0) { throw "备份文件大小为 0：$target" }

docker exec $Container sh -c "gzip -t $tmpGz"
if ($LASTEXITCODE -ne 0) { throw "gzip 完整性校验失败：$target" }

# 头三个字节是 gzip magic（1f 8b 08）。**必须查这个** ——
# 只查"文件非空"会漏掉"写进去的是错误信息"这种情况。
$head = [System.IO.File]::ReadAllBytes($target)[0..2]
if (-not ($head[0] -eq 0x1f -and $head[1] -eq 0x8b -and $head[2] -eq 0x08)) {
    throw "文件不是 gzip（魔数 $($head -join ',')）：$target"
}

# 里层确实是 SQL：解压后看有没有 pg_dump 的标志行
$looksLikeSql = docker exec $Container sh -c "zcat $tmpGz | head -c 4096 | grep -c 'PostgreSQL database dump' || true"
if ([int]$looksLikeSql -lt 1) { throw "解压后不像 pg_dump 输出：$target" }

docker exec $Container rm -f $tmpGz $tmpSql 2>&1 | Out-Null

$sha = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLower()
# 大小按量级选单位：这个库现在很小，写死 MB 会显示成 "0.0 MB"，
# 看起来像"备份了个空文件" —— 而这里恰恰是要让人相信备份是真的。
$sizeText = if ($size -ge 1MB) { "{0:N1} MB" -f ($size / 1MB) }
            elseif ($size -ge 1KB) { "{0:N1} KB" -f ($size / 1KB) }
            else { "$size 字节" }
Write-Host ("完成：{0}（{1}）" -f $name, $sizeText)
Write-Host "  sha256 = $sha"
Write-Host "  gzip 完整性、gzip 魔数、pg_dump 标志行：均已校验"

# ---- 4) 清理旧备份（只动本脚本自己产出的文件）----
if ($Keep -gt 0) {
    $mine = @(Get-ChildItem -LiteralPath $BackupDir -Filter "$Database-*.sql.gz" -File |
        Sort-Object Name -Descending)
    $removed = 0
    if ($mine.Count -gt $Keep) {
        $mine | Select-Object -Skip $Keep | ForEach-Object {
            Write-Host "清理旧备份：$($_.Name)"
            Remove-Item -LiteralPath $_.FullName -Force
            $removed++
        }
    }
    Write-Host "备份目录现有 $($mine.Count) 份，本次清理 $removed 份（上限 $Keep）"
}

Write-Host ''
Write-Host "下一步（**不做这一步，备份等于没有**）："
Write-Host "  .\scripts\恢复演练.ps1 -BackupFile `"$target`""
