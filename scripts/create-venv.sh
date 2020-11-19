#!/usr/bin/env bash
set -e

if [[ -z "${PIP_INSTALL}" ]]; then
    PIP_INSTALL='install'
fi

# Directory of *this* script
this_dir="$( cd "$( dirname "$0" )" && pwd )"
src_dir="$(realpath "${this_dir}/..")"

# -----------------------------------------------------------------------------

venv="${src_dir}/.venv"
download="${src_dir}/download"

# -----------------------------------------------------------------------------

function maybe_download {
    if [[ ! -s "$2" ]]; then
        mkdir -p "$(dirname "$2")"
        curl -sSfL -o "$2" "$1" || { echo "Can't download $1"; exit 1; }
        echo "$1 => $2"
    fi
}

larynx_file="${download}/larynx-0.2.0.tar.gz"
larynx_url='https://github.com/rhasspy/larynx/archive/v0.2.0.tar.gz'

maybe_download "${larynx_url}" "${larynx_file}"

tts_file="${download}/TTS-0.2.0.tar.gz"
tts_url='https://github.com/rhasspy/TTS/archive/v0.2.0.tar.gz'

maybe_download "${tts_url}" "${tts_file}"

# -----------------------------------------------------------------------------

: "${PYTHON=python3}"

# Create virtual environment
echo "Creating virtual environment at ${venv}"
rm -rf "${venv}"
"${PYTHON}" -m venv "${venv}"
source "${venv}/bin/activate"

# Install Python dependencies
echo "Installing Python dependencies"
pip3 ${PIP_INSTALL} --upgrade pip
pip3 ${PIP_INSTALL} --upgrade wheel setuptools

# Install local Rhasspy dependencies if available
grep '^rhasspy-' "${src_dir}/requirements.txt" | \
    xargs pip3 ${PIP_INSTALL} -f "${download}"

pip3 ${PIP_INSTALL} -r requirements.txt

# Install Larynx
echo 'Installing Larynx'
larynx_dir="${src_dir}/larynx"
rm -rf "${larynx_dir}"
mkdir -p "${larynx_dir}"
tar -C "${larynx_dir}" --strip-components=1 -xf "${larynx_file}"

# Turn it into a fake Python module
touch "${larynx_dir}/__init__.py"

# Add MozillaTTS fork as a submodule
tts_dir="${larynx_dir}/TTS"
rm -rf "${tts_dir}"
mkdir -p "${tts_dir}"
tar -C "${tts_dir}" --strip-components=1 -xf "${tts_file}"

# Install MozillaTTS fork dependencies
pushd "${tts_dir}"
pip3 install -r requirements.txt
python3 setup.py develop
popd

# Install Larynx dependencies
pushd "${larynx_dir}"
pip3 install -r requirements.txt
popd

# Optional development requirements
pip3 ${PIP_INSTALL} -r requirements_dev.txt || \
    echo "Failed to install development requirements"

# -----------------------------------------------------------------------------

echo "OK"
