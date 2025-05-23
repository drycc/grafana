
# Drycc Grafana

[![Build Status](https://woodpecker.drycc.cc/api/badges/drycc/grafana/status.svg)](https://woodpecker.drycc.cc/drycc/grafana)

Drycc (pronounced DAY-iss) Workflow is an open source Platform as a Service (PaaS) that adds a developer-friendly layer to any [Kubernetes][k8s-home] cluster, making it easy to deploy and manage applications on your own servers.

For more information about the Drycc Workflow, please visit the main project page at https://github.com/drycc/workflow.

We welcome your input! If you have feedback, please [submit an issue][issues]. If you'd like to participate in development, please read the "Development" section below and [submit a pull request][prs].

## Description

[Grafana](https://grafana.org/) is a graphing application built for time series data. It natively supports prometheus and provides great dashboarding support. This project is focused on provided a grafana installation that can be run within a kubernetes installation. The grafana application is agnostic to [Workflow][Workflow] and can be installed as a stand alone system with the monitoring suite.

## Development

The provided `Makefile` has various targets to help support building and publishing new images into a kubernetes cluster.

### Environment variables

There are a few key environment variables you should be aware of when interacting with the `make` targets.

* `BUILD_TAG` - The tag provided to the podman image when it is built (defaults to the git-sha)
* `SHORT_NAME` - The name of the image (defaults to `grafana`)
* `DRYCC_REGISTRY` - This is the registry you are using (default `registry.drycc.cc`)
* `IMAGE_PREFIX` - This is the account for the registry you are using (default `drycc`)

### Make targets

* `make build` - Build container image
* `make push` - Push container image to a registry
* `make upgrade` - Replaces the running grafana instance with a new one

The typical workflow will look something like this - `DRYCC_REGISTRY= IMAGE_PREFIX=foouser make build push upgrade`


[k8s-home]: http://kubernetes.io/
[issues]: https://github.com/drycc/monitor/issues
[prs]: https://github.com/drycc/monitor/pulls
[workflow]: https://github.com/drycc/workflow