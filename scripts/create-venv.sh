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
larynx_dir="${src_dir}/larynx"

if [[ ! -d "${larynx_dir}/TTS/TTS" ]]; then
    echo "Larynx not found. Try running 'git submodule update --init --recursive'"
    exit 1
fi

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

# Turn it into a fake Python module
touch "${larynx_dir}/__init__.py"

# Add MozillaTTS fork as a submodule
tts_dir="${larynx_dir}/TTS"

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
