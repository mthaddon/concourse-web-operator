# Copyright 2021 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

import unittest

from charm import ConcourseWebOperatorCharm
from ops.testing import Harness


class TestCharm(unittest.TestCase):
    def setUp(self):
        self.harness = Harness(ConcourseWebOperatorCharm)
        self.addCleanup(self.harness.cleanup)
        self.harness.begin()

    def test_external_url(self):
        self.assertEqual(self.harness.charm._external_url, "concourse-web")

    def test_concourse_layer(self):
        expected = {
            "services": {
                "concourse-web": {
                    "override": "replace",
                    "summary": "concourse web node",
                    "command": "/usr/local/bin/entrypoint.sh web",
                    "startup": "enabled",
                    "environment": {
                        "CONCOURSE_EXTERNAL_URL": "concourse-web",
                        "CONCOURSE_POSTGRES_HOST": None,
                        "CONCOURSE_POSTGRES_PORT": None,
                        "CONCOURSE_POSTGRES_DATABASE": None,
                        "CONCOURSE_POSTGRES_USER": None,
                        "CONCOURSE_POSTGRES_PASSWORD": None,
                        "CONCOURSE_SESSION_SIGNING_KEY": "/concourse-keys/session_signing_key",
                        "CONCOURSE_TSA_HOST_KEY": "/concourse-keys/tsa_host_key",
                        "CONCOURSE_TSA_AUTHORIZED_KEYS": "/concourse-keys/authorized_worker_keys",
                    },
                }
            },
        }
        self.assertEqual(self.harness.charm._concourse_layer(), expected)

    def test_concourse_key_locations(self):
        expected = {
            "CONCOURSE_SESSION_SIGNING_KEY": "/concourse-keys/session_signing_key",
            "CONCOURSE_TSA_HOST_KEY": "/concourse-keys/tsa_host_key",
            "CONCOURSE_TSA_AUTHORIZED_KEYS": "/concourse-keys/authorized_worker_keys",
        }
        self.assertEqual(self.harness.charm._concourse_key_locations, expected)
