# Local SQLite backup: hot-copies the database into backups\ and keeps the last 14.
# No API keys needed. (The upstream `eidolon backup` encrypts and uploads to object storage;
# this only keeps a local copy.)
# Usage: powershell -ExecutionPolicy Bypass -File scripts\backup-local.ps1

$repo = Split-Path -Parent $PSScriptRoot
$out  = Join-Path $repo 'backups'
New-Item -ItemType Directory -Force $out | Out-Null

$py = @'
import pathlib, sqlite3, time

dest_dir = pathlib.Path("/backups")
dest_dir.mkdir(parents=True, exist_ok=True)
target = dest_dir / ("eidolon-" + time.strftime("%Y%m%d-%H%M%S") + ".db")

src = sqlite3.connect("/data/eidolon.db")
dst = sqlite3.connect(target)
with dst:
    src.backup(dst)
dst.close()
src.close()
print("backup:", target.name, target.stat().st_size, "bytes")

for old in sorted(dest_dir.glob("eidolon-*.db"))[:-14]:
    old.unlink()
    print("pruned:", old.name)
'@

Push-Location $repo
try {
    $py | docker compose run --rm -T -v "${out}:/backups" backend python -
    $code = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $code
