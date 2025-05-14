#!/usr/bin/env bash
# build and install fleur and inpgen

set -euxo pipefail

FLEUR_REPO="https://iffgit.fz-juelich.de/fleur/fleur.git"
FLEUR_VERSION="MaX-R6.2"
DEPS=(libarpack2 libgomp1 libscalapack-openmpi2.2 openmpi-bin openmpi-common)
# this will be removed after compilation
BUILD_DEPS=(build-essential cmake doxygen gfortran git libarpack2-dev libopenblas-dev libopenmpi-dev libscalapack-mpi-dev libxc-dev libxml2-dev)
BUILD_DEPS_DO_NOT_REMOVE=(python3-minimal)
WORKDIR=$(mktemp -d)

export DEBIAN_FRONTEND=noninteractive
export FC=mpifort
export CXX=mpicxx
export CC=mpicc

# install deps
apt-get update
apt-get -y install "${DEPS[@]}" "${BUILD_DEPS[@]}" "${BUILD_DEPS_DO_NOT_REMOVE[@]}"

# build in ram
mount -t tmpfs -o size=6G fleur_build "$WORKDIR"
df -h

# build and install fleur
git clone --depth 1 --branch "$FLEUR_VERSION" "$FLEUR_REPO" "$WORKDIR"
pushd "$WORKDIR"
./configure.sh -libxc TRUE -hdf5 TRUE
pushd build
make -j 4
install -t /usr/local/bin fleur_MPI inpgen
ln -s /usr/local/bin/fleur_MPI /usr/local/bin/fleur
popd
popd

# cleanup
umount "$WORKDIR"
rm -rf "$WORKDIR"
apt-get -y remove "${BUILD_DEPS[@]}"
