# Concourse Web Operator

## Description

A Concourse installation is composed of a web node, a worker node, and a
PostgreSQL node. This charm deploys the web node.

## Usage

This charm requires a relation to PostgreSQL and to a Concourse worker node.
To deploy in a juju k8s model:

    juju deploy concourse-web
    juju deploy concourse-worker
    juju deploy postgresql-k8s
    juju deploy nginx-ingress-integrator
    # Add our relations
    juju relate concourse-web concourse-worker
    juju relate postgresql-k8s:db concourse-web
    juju relate nginx-ingress-integrator concourse-web

You can now visit `http://concourse-web` in a browser, assuming
`concourse-web` resolves to the IP of your k8s ingress provider (if you're on
MicroK8s this will be 127.0.0.1).

## Developing

Create and activate a virtualenv with the development requirements:

    virtualenv -p python3 venv
    source venv/bin/activate
    pip install -r requirements-dev.txt

## Testing

The Python operator framework includes a very nice harness for testing
operator behaviour without full deployment. Just `run_tests`:

    ./run_tests
