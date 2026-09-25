#!/bin/sh
# macOS bootstrap: uses system tools only; Python is in the verified release.
set -eu
umask 077
fail() { echo "安装失败：$*" >&2; exit 1; }
[ "$(uname -s)" = Darwin ] || fail '目前支持 macOS。'
case "$(uname -m)" in
  arm64) platform=macos-arm64 ;;
  x86_64) platform=macos-x86_64 ;;
  *) fail '不支持的处理器架构。' ;;
esac
# The pipe contains program text. Input and authorization must use the terminal.
if ! ( : < /dev/tty ) 2>/dev/null; then
  fail '请在交互式终端运行安装命令。'
fi
origin=https://dotasks.hanzeal.com
temporary=$(mktemp -d "${TMPDIR:-/tmp}/DoTasksCLI.XXXXXXXX")
trap 'rm -rf "$temporary"' EXIT HUP INT TERM
printf '%s\n' "[1/5] 检查系统……$platform"
printf '%s\n' '[2/5] 下载自带 Python 的生产安装包……'
curl -fsS --proto '=https' --connect-timeout 15 --max-time 60 --max-filesize 16384 \
  "$origin/downloads/cli/latest-$platform.json" -o "$temporary/manifest.json"
field() { plutil -extract "$1" raw -o - "$temporary/manifest.json"; }
version=$(field version)
digest=$(field sha256)
size=$(field size)
url=$(field url)
[ "$(field platform)" = "$platform" ] || fail '安装包架构不匹配。'
case "$version" in ''|*[!A-Za-z0-9_.-]*) fail '版本清单无效。' ;; esac
case "$digest" in ''|*[!0-9a-f]*) fail '摘要无效。' ;; esac
[ "${#digest}" -eq 64 ] || fail '摘要长度无效。'
case "$size" in ''|*[!0-9]*) fail '安装包大小无效。' ;; esac
[ "$size" -gt 0 ] && [ "$size" -le 157286400 ] || fail '安装包过大。'
[ "$url" = "/downloads/cli/$version/DoTasksCLI-$platform.zip" ] || fail '下载地址无效。'
curl -fsS --proto '=https' --connect-timeout 15 --max-time 600 --max-filesize "$size" \
  "$origin$url" -o "$temporary/release.zip"
printf '%s\n' '[3/5] 校验并解压……'
[ "$(wc -c < "$temporary/release.zip" | tr -d ' ')" = "$size" ] || fail '安装包大小不匹配。'
actual=$(shasum -a 256 "$temporary/release.zip" | awk '{print $1}')
[ "$actual" = "$digest" ] || fail '安装包摘要不匹配。'
unzip -Z -1 "$temporary/release.zip" > "$temporary/names"
awk '
  $0 !~ /^DoTasksCLI\// || $0 ~ /(^|\/)\.\.?($|\/)/ || $0 ~ /\\/ || $0 ~ /\/\// || seen[$0]++ {exit 1}
  END {if (NR == 0 || NR > 10000) exit 1}
' "$temporary/names" || fail '安装包包含不安全的路径。'
unzip -Z -l "$temporary/release.zip" > "$temporary/entries"
# Only regular files/directories; links and device nodes are not accepted.
awk '/^[bclps]/ {exit 1}' "$temporary/entries" || fail '安装包包含不安全的文件类型。'
unzip -Z -t "$temporary/release.zip" > "$temporary/totals"
awk '$2 == "files," && $4 == "bytes" && $5 == "uncompressed," && $3 <= 629145600 {ok=1} END {exit !ok}' \
  "$temporary/totals" || fail '解压后安装包过大或格式无效。'
unzip -q "$temporary/release.zip" -d "$temporary"
[ "$(plutil -extract version raw -o - "$temporary/DoTasksCLI/runtime/release.json")" = "$version" ] || fail '包内版本与清单不一致。'
[ "$(plutil -extract platform raw -o - "$temporary/DoTasksCLI/runtime/release.json")" = "$platform" ] || fail '包内架构与清单不一致。'
printf '%s\n' '[4/5] 安装 CLI……'
sh "$temporary/DoTasksCLI/install-cli" "$@" < /dev/tty
printf '%s\n' '[5/5] 初始化并连接本机 Codex……'
if "$HOME/.local/bin/dotasks" init < /dev/tty; then
  exit 0
else
  code=$?
  echo 'CLI 已安装，初始化尚未完成。运行 ~/.local/bin/dotasks init 继续。' >&2
  exit "$code"
fi
