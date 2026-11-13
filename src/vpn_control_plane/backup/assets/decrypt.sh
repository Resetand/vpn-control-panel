#!/bin/sh
# Decrypts every <part>/state.tar.gz.age of this backup into <part>/state/.
# The key is the private SSH key whose fingerprint is in <part>/manifest.txt: found in
# ~/.ssh automatically, or given with -i.
# Usage: sh decrypt.sh [-i private_key]
set -eu
cd "$(dirname "$0")"

identity=
while getopts i: opt; do
    case $opt in
        i) identity=$OPTARG ;;
        *) echo "usage: sh decrypt.sh [-i private_key]" >&2; exit 2 ;;
    esac
done
command -v age >/dev/null || { echo "age is required: brew install age / apt install age" >&2; exit 1; }

manifest_value() {
    sed -n "s/^$2=//p" "$1/manifest.txt" 2>/dev/null
}

# Matching by fingerprint picks the key before decrypting, so a passphrase is asked once.
find_identity() {
    for key in "$HOME"/.ssh/id_*; do
        case $key in *.pub) continue ;; esac
        [ -f "$key" ] || continue
        public=$key.pub
        [ -f "$public" ] || public=$key
        if [ "$(ssh-keygen -l -f "$public" 2>/dev/null | awk '{print $2}')" = "$1" ]; then
            echo "$key"
            return 0
        fi
    done
    return 1
}

# One part that cannot be decrypted must not keep the others from being restored.
decrypted=0
failed=
for archive in */state.tar.gz.age; do
    [ -f "$archive" ] || continue
    part=${archive%/state.tar.gz.age}
    key=$identity
    if [ -z "$key" ]; then
        wanted=$(manifest_value "$part" recipient_fingerprint)
        if ! key=$(find_identity "$wanted"); then
            echo "$part: no key in ~/.ssh matches ${wanted:-<no fingerprint in manifest>}" \
                "($(manifest_value "$part" recipient_comment)); pass it with -i" >&2
            failed="$failed $part"
            continue
        fi
    fi
    rm -rf "$part/state"
    mkdir -p "$part/state"
    if age -d -i "$key" "$archive" > "$part/state.tar.gz" && tar -xzf "$part/state.tar.gz" -C "$part/state"; then
        echo "$part: decrypted into $part/state with $key"
        decrypted=$((decrypted + 1))
    else
        echo "$part: decryption with $key failed" >&2
        failed="$failed $part"
    fi
    rm -f "$part/state.tar.gz"
done
if [ -n "$failed" ]; then
    echo "not decrypted:$failed" >&2
    exit 1
fi
[ "$decrypted" -gt 0 ] || { echo "no */state.tar.gz.age next to this script" >&2; exit 1; }
echo "next: follow <part>/RESTORE.md for each part"
