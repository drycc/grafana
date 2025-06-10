SHELL := /bin/bash

# grafana share/use the following targets/exports
SHORT_NAME ?= grafana
SHELL_SCRIPTS = rootfs/usr/share/grafana/oauth2

BUILD_TAG ?= git-$(shell git rev-parse --short HEAD)
DRYCC_REGISTRY ?= ${DEV_REGISTRY}
IMAGE_PREFIX ?= drycc
PLATFORM ?= linux/amd64,linux/arm64

include includes.mk
include versioning.mk

TEST_ENV_PREFIX := podman run --rm -v ${CURDIR}:/bash -w /bash ${DEV_REGISTRY}/drycc/python-dev

build: podman-build
push: podman-push
deploy: check-kubectl podman-build podman-push install

podman-build:
	podman build --build-arg CODENAME=${CODENAME} -t ${IMAGE} rootfs
	podman tag ${IMAGE} ${MUTABLE_IMAGE}

podman-buildx:
	podman buildx build --build-arg CODENAME=${CODENAME} --platform ${PLATFORM} -t ${IMAGE} rootfs --push

clean: check-podman
	podman rmi $(IMAGE)
	
test: test-style

test-style:
	${TEST_ENV_PREFIX} flake8 --show-source --max-line-length=100 $(SHELL_SCRIPTS)

.PHONY: build push podman-build clean upgrade deploy test test-style

