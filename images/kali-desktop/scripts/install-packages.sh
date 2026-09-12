#!/usr/bin/env bash
set -Eeuo pipefail
case "${CYBERPOD_TOOL_PROFILE:-mvp}" in
  base) profile_files=(/opt/cyberpod/packages/base.txt);;
  mvp) profile_files=(/opt/cyberpod/packages/base.txt /opt/cyberpod/packages/mvp.txt);;
  *) echo 'Unsupported tool profile' >&2; exit 64;;
esac
package_names=()
while IFS= read -r package_name; do
  [[ -z "$package_name" || "$package_name" == \#* ]] && continue
  package_names+=("$package_name")
done < <(cat "${profile_files[@]}")
read -r -a extra_packages <<< "${CYBERPOD_EXTRA_PACKAGES:-}"
package_names+=("${extra_packages[@]}")
for package_name in "${package_names[@]}"; do
  [[ "$package_name" =~ ^[a-z0-9][a-z0-9+.-]*(:[a-z0-9]+)?(=[A-Za-z0-9.+:~_-]+)?$ ]] || {
    echo 'Invalid package name/version in build input' >&2; exit 64;
  }
done
apt-get update
apt-get install -y --no-install-recommends "${package_names[@]}"
apt-get clean
rm -rf /var/lib/apt/lists/*
