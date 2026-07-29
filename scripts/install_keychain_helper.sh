#!/bin/zsh
set -euo pipefail

script_dir=${0:A:h}
project_root=${script_dir:h}
project_path="${project_root}/macos/XMentionKeychainHelper.xcodeproj"
derived_data="${project_root}/var/keychain-helper-derived"
products_dir="${derived_data}/Build/Products/Release"
app_path="${products_dir}/XMentionKeychainHelper.app"
apps_root="${project_root}/var/keychain-helper-apps"
staging_dir=""
temporary_link=""

cleanup() {
  if [[ -n "${staging_dir}" ]] &&
     [[ -d "${staging_dir}" ]] &&
     [[ "${staging_dir}" == "${apps_root}/.install."* ]]; then
    /bin/rm -rf "${staging_dir}"
  fi
  if [[ -n "${temporary_link}" ]] &&
     [[ -L "${temporary_link}" ]] &&
     [[ "${temporary_link}" == "${project_root}/var/keychain-helper.next."* ]]
  then
    /bin/rm -f "${temporary_link}"
  fi
}
trap cleanup EXIT

/usr/bin/xcodebuild \
  -project "${project_path}" \
  -scheme XMentionKeychainHelper \
  -configuration Release \
  -derivedDataPath "${derived_data}" \
  -allowProvisioningUpdates \
  -allowProvisioningDeviceRegistration \
  build

if [[ ! -x "${app_path}/Contents/MacOS/XMentionKeychainHelper" ]]; then
  print -u2 "Signed Keychain helper executable is missing."
  exit 1
fi

/usr/bin/codesign --verify --deep --strict "${app_path}"

if [[ ! -f "${app_path}/Contents/embedded.provisionprofile" ]]; then
  print -u2 "Embedded provisioning profile is missing."
  exit 1
fi

python3 "${project_root}/scripts/keychain_bundle.py" \
  "${app_path}/Contents/MacOS/XMentionKeychainHelper"

executable_sha=$(
  /usr/bin/shasum -a 256 \
    "${app_path}/Contents/MacOS/XMentionKeychainHelper" |
    /usr/bin/awk '{print $1}'
)
install_dir="${apps_root}/${executable_sha}"
installed_app="${install_dir}/XMentionKeychainHelper.app"
installed_executable="${installed_app}/Contents/MacOS/XMentionKeychainHelper"

/bin/mkdir -p "${apps_root}"
if [[ -d "${installed_app}" ]] &&
   python3 "${project_root}/scripts/keychain_bundle.py" \
     "${installed_executable}" >/dev/null; then
  :
else
  staging_dir=$(/usr/bin/mktemp -d "${apps_root}/.install.XXXXXX")
  staging_app="${staging_dir}/XMentionKeychainHelper.app"
  /usr/bin/ditto "${app_path}" "${staging_app}"
  python3 "${project_root}/scripts/keychain_bundle.py" \
    "${staging_app}/Contents/MacOS/XMentionKeychainHelper" >/dev/null
  if [[ -d "${install_dir}" ]]; then
    invalid_suffix=$(/bin/date -u +%Y%m%dT%H%M%SZ)
    /bin/mv \
      "${install_dir}" \
      "${install_dir}.invalid-${invalid_suffix}"
  fi
  /bin/mv "${staging_dir}" "${install_dir}"
  staging_dir=""
fi

config_path="${project_root}/config.json"
if [[ -f "${config_path}" ]]; then
  keychain_service=$(
    python3 -c \
      'import json,sys; print(json.load(open(sys.argv[1])).get("keychain_service",""))' \
      "${config_path}"
  )
  keychain_account=$(
    python3 -c \
      'import json,sys; print(json.load(open(sys.argv[1])).get("keychain_account",""))' \
      "${config_path}"
  )
  if [[ -n "${keychain_service}" ]] && [[ -n "${keychain_account}" ]]; then
    "${installed_executable}" \
      migrate-if-present \
      "${keychain_service}" \
      "${keychain_account}"
  fi
fi

link_target="keychain-helper-apps/${executable_sha}/XMentionKeychainHelper.app/Contents/MacOS/XMentionKeychainHelper"
temporary_link="${project_root}/var/keychain-helper.next.$$"
if [[ -e "${project_root}/var/keychain-helper" ]] &&
   [[ ! -L "${project_root}/var/keychain-helper" ]]; then
  legacy_sha=$(
    /usr/bin/shasum -a 256 "${project_root}/var/keychain-helper" |
      /usr/bin/awk '{print $1}'
  )
  /bin/cp -p \
    "${project_root}/var/keychain-helper" \
    "${project_root}/var/keychain-helper.legacy-${legacy_sha}"
fi
/bin/rm -f "${temporary_link}"
/bin/ln -s "${link_target}" "${temporary_link}"
/bin/mv -f "${temporary_link}" "${project_root}/var/keychain-helper"

print "${installed_executable}"
