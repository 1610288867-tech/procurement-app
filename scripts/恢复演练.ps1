<#
.SYNOPSIS
    恢复演练：把一份备份**真的恢复进一个临时库**，量出耗时，然后删掉。

.DESCRIPTION
    **备份没恢复过就等于没有备份。** 本脚本回答三个只有真做一遍才知道的问题：

        1. 这份备份**能用吗**（不是"文件在"，而是"恢复得进去"）？
        2. 恢复**要多久**（RTO 的实测下界，不再是拍脑袋）？
        3. 恢复出来的**数据和源库对得上吗**（表数、关键表行数）？

    ============================ 三条纪律 ============================
    ① **绝不恢复进正是那个库**：脚本拒绝 `-TargetDb` 等于源库名，防止手滑。
       演练用的是 `{源库}_restore_check_{时间戳}`，跑完一律 DROP。
    ② **回读用 `-tAc` 单条查询**，把 `psql` 的输出当**数据**处理；
       恢复本身用 `-v ON_ERROR_STOP=1` —— 那里要的是"出错就停"。
       两种场景要的是相反的语义，所以分成两次调用。
    ③ **原库是只读的**：本脚本对源库只做 SELECT（数行数），
       所有写操作都发生在临时库上。

.PARAMETER BackupFile
    要演练的备份文件（`备份数据库.ps1` 的产物，`.sql.gz`）。不传就取备份目录里最新的一份。

.PARAMETER BackupDir
    备份目录，默认 `D:\develop\procurement\backups`（只在不传 -BackupFile 时用）。

.PARAMETER Database
    源库名，默认 `procurement`。**它同时是要核对行数的库。**

.PARAMETER KeepDrill
    演练结束后**保留**临时库（排查用）。默认删掉。

.EXAMPLE
    .\scripts\恢复演练.ps1

.EXAMPLE
    .\scripts\恢复演练.ps1 -BackupFile D:\develop\procurement\backups\procurement-20261015T130312Z.sql.gz

.NOTES
    会 CREATE / DROP 一个临时数据库；**不碰源库的数据**，不删任何备份文件。
#>
[CmdletBinding()]
param(
    [string]$BackupFile,

    [string]$BackupDir = 'D:\develop\procurement\backups',

    [string]$Database = 'procurement',

    [string]$Container = 'procurement-db',

    [string]$User = 'app',

    [switch]$KeepDrill
)

$ErrorActionPreference = 'Stop'

function Invoke-Psql {
    param([Parameter(Position = 0)][string]$Sql, [string]$DbName = 'postgres', [switch]$StopOnError)
    # ⚠️ 不叫 `$args`：那是 PowerShell 的自动变量（本仓库守则里点过名）。
    # ⚠️ 两个分支的**语义必须相反**：
    #    `-tAc` 用于**回读**（把输出当数据，不加 ON_ERROR_STOP）
    #    `-v ON_ERROR_STOP=1 -f` 用于**恢复**（出错必须停，否则会"恢复了一半还说成功"）
    #    用 `-f 文件` 而不是 `\i` 反斜杠命令：后者是交互式元命令，
    #    经 `-c` / 管道传入时行为依赖上下文，不如参数可靠。
    $argv = if ($StopOnError) {
        @('exec', $Container, 'psql', '-U', $User, '-d', $DbName, '-v', 'ON_ERROR_STOP=1', '-q', '-f', $Sql)
    } else {
        @('exec', $Container, 'psql', '-U', $User, '-d', $DbName, '-tAc', $Sql)
    }
    $out = & docker @argv 2>&1
    return @{ Code = $LASTEXITCODE; Out = $out }
}

# ---- 前置检查 ----
$state = docker inspect $Container --format '{{.State.Status}}' 2>&1
if ($LASTEXITCODE -ne 0 -or $state -notmatch 'running') {
    throw "容器 $Container 不在运行（状态：$state）。先跑 .\scripts\dev-up.ps1"
}

if (-not $BackupFile) {
    $latest = Get-ChildItem -LiteralPath $BackupDir -Filter "$Database-*.sql.gz" -File -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending | Select-Object -First 1
    if (-not $latest) { throw "备份目录里没有 $Database-*.sql.gz：$BackupDir（先跑 .\scripts\备份数据库.ps1）" }
    $BackupFile = $latest.FullName
    Write-Host "未指定 -BackupFile，取最新一份：$($latest.Name)"
}
if (-not (Test-Path -LiteralPath $BackupFile)) { throw "备份文件不存在：$BackupFile" }

$stamp = (docker exec $Container date -u '+%Y%m%dT%H%M%SZ' 2>&1).Trim()
# ⚠️ **必须转小写**（实测踩到）：PostgreSQL 把**未加引号**的标识符折成小写，
#    所以 `CREATE DATABASE x_check_20261015T1303Z` 建出来的是 `x_check_20261015t1303z`，
#    而随后用原样大小写去连 → `FATAL: database "…Z" does not exist`。
#    报错指向"库不存在"，看起来像建库没成功，其实是**大小写不匹配**。
$targetDb = ("${Database}_restore_check_$stamp").ToLowerInvariant()
if ($targetDb -eq $Database) { throw "临时库名与源库相同，拒绝执行" }

$inContainer = "/tmp/dsh-restore-$stamp.sql.gz"
$drillName = Split-Path -Leaf $BackupFile
$drillSize = (Get-Item -LiteralPath $BackupFile).Length

Write-Host ''
Write-Host '================ 恢复演练 ================'
Write-Host "备份文件 : $drillName（$(if ($drillSize -ge 1KB) { '{0:N1} KB' -f ($drillSize/1KB) } else { "$drillSize 字节" })）"
Write-Host "临时库   : $targetDb"
Write-Host ''

# ---- 1) 拷进容器并解压（管道发生在容器内部）----
docker cp $BackupFile "${Container}:${inContainer}"
if ($LASTEXITCODE -ne 0) { throw "docker cp 失败" }
$plain = $inContainer -replace '\.gz$', ''
docker exec $Container sh -c "set -e; gzip -dc $inContainer > $plain"
if ($LASTEXITCODE -ne 0) { throw "解压失败：$inContainer" }

# ---- 建临时库 ----
# ⚠️ 全部用具名 `-Sql`：本脚本第一版写成位置参数，结果 `Invoke-Psql $plain`
#    把解压出来的 SQL 路径绑到了**脚本自己的** `-BackupFile` 上（同名参数遮蔽），
#    于是建库没建、恢复报"database does not exist"。**别用位置参数调它。**
$r = Invoke-Psql -Sql "DROP DATABASE IF EXISTS $targetDb"
$r = Invoke-Psql -Sql "CREATE DATABASE $targetDb"
if ($r.Code -ne 0) { throw "建库失败：$($r.Out -join "`n")" }
Write-Host "已建临时库：$targetDb"

# 从这里开始，任何失败都要把临时库收掉 —— 否则库里会攒下一堆 `_restore_check_*`
try {
    # ---- 3) 恢复（计时）----
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $r = Invoke-Psql -Sql $plain -DbName $targetDb -StopOnError
    $sw.Stop()
    if ($r.Code -ne 0) { throw "恢复失败（退出码 $($r.Code)）：`n$($r.Out -join "`n")" }
    Write-Host ("恢复完成，耗时 {0:N1} 秒" -f $sw.Elapsed.TotalSeconds)

    # ---- 4) 核对：这是演练与"只是看了眼文件"的分界线 ----
    $q = "select count(*) from information_schema.tables where table_schema='public' and table_type='BASE TABLE'"
    $tables = (Invoke-Psql -Sql $q -DbName $targetDb).Out
    $srcTables = (Invoke-Psql -Sql $q -DbName $Database).Out

    $checkTables = @('purchase_requests', 'purchase_items', 'approval_flows', 'approval_tasks',
                     'budgets', 'budget_transactions', 'request_events', 'idempotency_records',
                     'knowledge_docs', 'knowledge_chunks', 'chat_messages', 'users')
    $rows = foreach ($t in $checkTables) {
        $a = (Invoke-Psql -Sql "select count(*) from $t" -DbName $targetDb).Out
        $b = (Invoke-Psql -Sql "select count(*) from $t" -DbName $Database).Out
        [pscustomobject]@{ 表 = $t; 演练库 = "$a".Trim(); 源库 = "$b".Trim(); 一致 = ("$a".Trim() -eq "$b".Trim()) }
    }

    Write-Host ''
    Write-Host '---- 结构核对 ----'
    Write-Host ("public 表数：演练库 {0} / 源库 {1}  {2}" -f "$tables".Trim(), "$srcTables".Trim(),
        $(if ("$tables".Trim() -eq "$srcTables".Trim()) { '✅' } else { '❌ 不一致' }))
    Write-Host ''
    Write-Host '---- 关键表行数核对 ----'
    $rows | Format-Table -AutoSize
    $bad = @($rows | Where-Object { -not $_.一致 })
    if ($bad.Count -eq 0) {
        Write-Host "✅ 12 张关键表行数全部一致"
    } else {
        Write-Host "❌ 有 $($bad.Count) 张表行数不一致：$(($bad.表) -join ', ')"
    }
}
finally {
    # ---- 5) 收尾：无论成功失败都收掉临时库与容器内中转文件 ----
    if ($KeepDrill -and $?) {
        Write-Host ''
        Write-Host "已保留临时库（-KeepDrill）：$targetDb"
        Write-Host "用完删掉：docker exec $Container psql -U $User -d postgres -c `"DROP DATABASE $targetDb`""
    } else {
        $r = Invoke-Psql -Sql "DROP DATABASE IF EXISTS $targetDb"
        if ($r.Code -eq 0) { Write-Host ''; Write-Host "已删除临时库：$targetDb" }
        else { Write-Warning "临时库删除失败，请手工清理：$targetDb" }
    }
    docker exec $Container rm -f $inContainer $plain 2>&1 | Out-Null
}

Write-Host ''
Write-Host '================ 演练结论 ================'
Write-Host ("RTO 实测下界（仅恢复耗时，不含故障发现与切换）：{0:N1} 秒" -f $sw.Elapsed.TotalSeconds)
Write-Host ''
Write-Host '⚠️ 这份演练**不覆盖**：备份文件本身丢失（备份与本机同盘）、'
Write-Host '   增量/时间点恢复（需要 WAL 归档，见 RISKS 一.11）。'
