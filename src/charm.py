#!/usr/bin/env python3
# Copyright 2021 Canonincal Ltd.
# See LICENSE file for licensing details.

import logging
import os
import subprocess
from pathlib import Path

import kubernetes

from tempfile import NamedTemporaryFile

import ops.lib

from ops.charm import CharmBase
from ops.framework import StoredState
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus
from ops.pebble import ConnectionError

from charms.nginx_ingress_integrator.v0.ingress import IngressRequires

pgsql = ops.lib.use("pgsql", 1, "postgresql-charmers@lists.launchpad.net")

logger = logging.getLogger(__name__)


def _core_v1_api():
    """Use the v1 k8s API."""
    cl = kubernetes.client.ApiClient()
    return kubernetes.client.CoreV1Api(cl)


class ConcourseWebOperatorCharm(CharmBase):
    _authed = False
    _restart_required = False
    _stored = StoredState()

    def __init__(self, *args):
        super().__init__(*args)
        self.framework.observe(self.on.config_changed, self._on_config_changed)
        self.ingress = IngressRequires(self, self.ingress_config)

        self._stored.set_default(
            concourse_worker=False,
            concourse_worker_pub_keys=[],
            db_name=None,
            db_host=None,
            db_port=None,
            db_user=None,
            db_password=None,
            db_conn_str=None,
            db_uri=None,
            db_ro_uris=[]
        )
        self.db = pgsql.PostgreSQLClient(self, 'db')
        self.framework.observe(self.db.on.database_relation_joined, self._on_database_relation_joined)
        self.framework.observe(self.db.on.master_changed, self._on_master_changed)
        self.framework.observe(self.db.on.standby_changed, self._on_standby_changed)

        self.framework.observe(self.on.concourse_worker_relation_joined, self._on_concourse_worker_relation_joined)
        self.framework.observe(self.on.concourse_worker_relation_changed, self._on_concourse_worker_relation_changed)
        self.framework.observe(self.on.concourse_worker_relation_broken, self._on_concourse_worker_relation_broken)

        self.framework.observe(self.on["peer"].relation_created, self._on_peer_relation_changed)
        self.framework.observe(self.on["peer"].relation_joined, self._on_peer_relation_changed)

    def _on_peer_relation_changed(self, event):
        """Peer relation event handler."""
        # If we don't have our concourse keys yet, check if there's any already
        # defined on the peer relation, and if not, and we're the leader, create
        # them.

        # First, check if they already exist on disk, if so, exit. We're
        # checking in the charm container and we'll "mirror" content from here
        # into the workload container.
        keys_found = True
        for _, location in self._concourse_web_key_locations.items():
            if not os.path.exists(location):
                keys_found = False
        if keys_found:
            logger.info("Found all concourse keys on disk.")
            return

        # Now check if we have them already defined on the relation. If so,
        # generate them from that.
        container = self.unit.get_container("concourse-web")
        keys_found = True
        for key, location in self._concourse_web_key_locations.items():
            key = event.relation.data[event.app].get(key)
            if key:
                # We could just write locally, or we can use `container.push`
                # since /concourse-keys is mounted in the charm and workload
                # containers.
                try:
                    container.push(
                        location,
                        key,
                        make_dirs=True,
                    )
                except ConnectionError:
                    event.defer()
                    return
            else:
                keys_found = False
        if keys_found:
            logger.info("Found all concourse keys via the relation.")
            # Trigger our config changed hook again.
            self._restart_required = True
            self.on.config_changed.emit()
            return

        # Assuming they're not available on the relation, we need to generate
        # them ourselves, but we can only do that if we're the leader.
        if not self.unit.is_leader():
            logger.info("We're not the leader, so we don't want to try to create concourse keys.")
            event.defer()
            return

        # We should do this via a one-shot command on the workload container,
        # but that's not possible yet, so let's pull the binary from the
        # workload container, execute our command and push the files to the
        # workload container. https://bugs.launchpad.net/juju/+bug/1923822.
        # XXX: Need to generate the keys once and distribute among peers.
        #      See https://concourse-ci.org/concourse-web.html#web-running
        #      Keys can be generated per https://github.com/concourse/concourse-docker
        #      using (from the deployed image):
        #        /usr/local/concourse/bin/concourse generate-key -t rsa -f ./session_signing_key
        #        /usr/local/concourse/bin/concourse generate-key -t ssh -f ./tsa_host_key
        #        /usr/local/concourse/bin/concourse generate-key -t ssh -f ./worker_key
        try:
            concourse_binary_path = self._get_concourse_binary_path()
        except ConnectionError:
            event.defer()
            return

        # CONCOURSE_SESSION_SIGNING_KEY.
        subprocess.run([concourse_binary_path, "generate-key", "-t", "rsa", "-f",
                        self._concourse_key_locations["CONCOURSE_SESSION_SIGNING_KEY"]])
        # No need to push to the container, as we've mounted /concourse-keys at
        # the same location. So just publish on the relation so peers can consume.
        with open(self._concourse_key_locations["CONCOURSE_SESSION_SIGNING_KEY"], 'r') as session_signing_key:
            session_signing_key_data = session_signing_key.read()
        event.relation.data[self.app]["CONCOURSE_SESSION_SIGNING_KEY"] = session_signing_key_data
        logger.info("Set CONCOURSE_SESSION_SIGNING_KEY on the concourse-web peer relation.")

        # CONCOURSE_TSA_HOST_KEY and CONCOURSE_TSA_HOST_KEY_PUB.
        # Not quite clear yet on whether we should use the same one for all web nodes yet...
        subprocess.run([concourse_binary_path, "generate-key", "-t", "ssh", "-f",
                        self._concourse_key_locations["CONCOURSE_TSA_HOST_KEY"]])
        # No need to push to the container, as we've mounted /concourse-keys at
        # the same location. So just publish on the relation so peers can consume.
        with open(self._concourse_key_locations["CONCOURSE_TSA_HOST_KEY"], 'r') as tsa_host_key:
            tsa_host_key_data = tsa_host_key.read()
        event.relation.data[self.app]["CONCOURSE_TSA_HOST_KEY"] = tsa_host_key_data
        logger.info("Set CONCOURSE_TSA_HOST_KEY on the concourse-web peer relation.")
        with open(self._concourse_key_locations["CONCOURSE_TSA_HOST_KEY_PUB"], 'r') as tsa_host_key_pub:
            tsa_host_key_pub_data = tsa_host_key_pub.read()
        event.relation.data[self.app]["CONCOURSE_TSA_HOST_KEY_PUB"] = tsa_host_key_pub_data
        logger.info("Set CONCOURSE_TSA_HOST_KEY_PUB on the concourse-web peer relation.")

        # Trigger our config changed hook again.
        self._restart_required = True
        self.on.config_changed.emit()

    def _get_concourse_binary_path(self):
        container = self.unit.get_container("concourse-web")
        with NamedTemporaryFile(delete=False) as temp:
            temp.write(container.pull("/usr/local/concourse/bin/concourse", encoding=None).read())
            temp.flush()
            logger.info("Wrote concourse binary to %s", temp.name)

            # Make it executable
            os.chmod(temp.name, 0o777)
        return temp.name

    @property
    def _concourse_web_key_locations(self):
        return {
            "CONCOURSE_SESSION_SIGNING_KEY": "/concourse-keys/session_signing_key",
            "CONCOURSE_TSA_HOST_KEY": "/concourse-keys/tsa_host_key",
            # This one isn't used by the container, but we set it for convenience.
            "CONCOURSE_TSA_HOST_KEY_PUB": "/concourse-keys/tsa_host_key.pub",
        }

    @property
    def _concourse_worker_key_locations(self):
        return {
            "CONCOURSE_TSA_AUTHORIZED_KEYS": "/concourse-keys/authorized_worker_keys",
        }

    @property
    def _concourse_key_locations(self):
        key_locations = self._concourse_web_key_locations
        key_locations.update(self._concourse_worker_key_locations)
        return key_locations

    @property
    def _k8s_service_name(self):
        """Return a service name for the use creating a k8s service."""
        return "{}-ssh-service".format(self.app.name)

    def k8s_auth(self):
        """Authenticate to kubernetes."""
        if self._authed:
            return

        kubernetes.config.load_incluster_config()

        self._authed = True

    def _get_k8s_service(self):
        """Get a K8s service definition."""
        return kubernetes.client.V1Service(
            api_version="v1",
            kind="Service",
            metadata=kubernetes.client.V1ObjectMeta(name=self._k8s_service_name),
            spec=kubernetes.client.V1ServiceSpec(
                selector={"app.kubernetes.io/name": self.app.name},
                ports=[
                    kubernetes.client.V1ServicePort(
                        name="tcp-{}".format(2222),
                        port=2222,
                        target_port=2222,
                    )
                ],
            ),
        )

    def _ensure_k8s_service(self):
        """Create or update relevant k8s service."""
        self.k8s_auth()
        api = _core_v1_api()
        body = self._get_k8s_service()
        services = api.list_namespaced_service(namespace=self.model.name)
        if self._k8s_service_name in [x.metadata.name for x in services.items]:
            api.patch_namespaced_service(
                name=self._k8s_service_name,
                namespace=self.model.name,
                body=body,
            )
            logger.info(
                "Service updated in namespace %s with name %s",
                self.model.name,
                self._k8s_service_name,
            )
        else:
            api.create_namespaced_service(
                namespace=self.model.name,
                body=body,
            )
            logger.info(
                "Service created in namespace %s with name %s",
                self.model.name,
                self._k8s_service_name,
            )

    def _on_concourse_worker_relation_joined(self, event):
        self._stored.concourse_worker = True
        # Do we need to do this once per app, or once per unit?
        if self.unit.is_leader():
            container = self.unit.get_container("concourse-web")
            try:
                tsa_host_key_pub = container.pull(
                    self._concourse_web_key_locations["CONCOURSE_TSA_HOST_KEY_PUB"]
                ).read()
            except ConnectionError:
                event.defer()
                return
            # XXX: We'll need to react to leader elected here and update as appropriate.
            leader_address = str(self.model.get_binding(event.relation).network.bind_address)
            event.relation.data[self.app]["TSA_HOST"] = leader_address
            logger.info("Set TSA_HOST to %s on the concourse_worker relation.", leader_address)
            event.relation.data[self.app]["CONCOURSE_TSA_HOST_KEY_PUB"] = tsa_host_key_pub
            logger.info("Set CONCOURSE_TSA_HOST_KEY_PUB on the concourse_worker relation.")
        # Trigger our config changed hook again.
        self.on.config_changed.emit()

    def _on_concourse_worker_relation_changed(self, event):
        changed = False
        # XXX: We really want to iterate over all the units here.
        if event.unit:
            worker_key_pub = event.relation.data[event.unit].get("WORKER_KEY_PUB")
            if worker_key_pub and worker_key_pub not in self._stored.concourse_worker_pub_keys:
                changed = True
                self._stored.concourse_worker_pub_keys.append(worker_key_pub)
        if changed:
            with open(self._concourse_key_locations["CONCOURSE_TSA_AUTHORIZED_KEYS"], "w") as authorized_keys:
                authorized_keys.write("\n".join(self._stored.concourse_worker_pub_keys))
                logger.info("Updated CONCOURSE_TSA_AUTHORIZED_KEYS file")
            # Trigger our config changed hook again.
            self._restart_required = True
            self.on.config_changed.emit()

    def _on_concourse_worker_relation_broken(self, _):
        self._stored.concourse_worker = False
        # Trigger our config changed hook again.
        self.on.config_changed.emit()

    def _on_database_relation_joined(self, event: pgsql.DatabaseRelationJoinedEvent):
        if self.model.unit.is_leader():
            # Provide requirements to the PostgreSQL server.
            event.database = 'concourse'
        elif event.database != 'concourse':
            # Leader has not yet set requirements. Defer, incase this unit
            # becomes leader and needs to perform that operation.
            event.defer()
            return

    def _on_master_changed(self, event: pgsql.MasterChangedEvent):
        if event.database != 'concourse':
            # Leader has not yet set requirements. Wait until next event,
            # or risk connecting to an incorrect database.
            return

        # The connection to the primary database has been created,
        # changed or removed. More specific events are available, but
        # most charms will find it easier to just handle the Changed
        # events. event.master is None if the master database is not
        # available, or a pgsql.ConnectionString instance.
        self._stored.db_name = event.database
        self._stored.db_host = event.master.host if event.master else None
        self._stored.db_port = event.master.port if event.master else None
        self._stored.db_user = event.master.user if event.master else None
        self._stored.db_password = event.master.password if event.master else None
        self._stored.db_conn_str = None if event.master is None else event.master.conn_str

        # Trigger our config changed hook again.
        self.on.config_changed.emit()

    def _on_standby_changed(self, event: pgsql.StandbyChangedEvent):
        if event.database != 'concourse':
            # Leader has not yet set requirements. Wait until next event,
            # or risk connecting to an incorrect database.
            return

        # Charms needing access to the hot standby databases can get
        # their connection details here. Applications can scale out
        # horizontally if they can make use of the read only hot
        # standby replica databases, rather than only use the single
        # master. event.stanbys will be an empty list if no hot standby
        # databases are available.
        self._stored.db_ro_uris = [c.uri for c in event.standbys]

    def _on_config_changed(self, event):
        try:
            self._ensure_k8s_service()
        except kubernetes.client.exceptions.ApiException as e:
            if e.status == 403:
                logger.error(
                    "Insufficient permissions to create the k8s service, "
                    "will request `juju trust` to be run"
                )
                self.unit.status = BlockedStatus(
                    "Insufficient permissions, try: `juju trust {} --scope=cluster`".format(
                        self.app.name
                    )
                )
                return
            else:
                raise
        required_relations = []
        if not self._stored.db_conn_str:
            required_relations.append("PostgreSQL")
        if not self._stored.concourse_worker:
            required_relations.append("Concourse Worker")
        if required_relations:
            self.unit.status = BlockedStatus(
                "The following relations are required: {}".format(", ".join(required_relations))
            )
            return
        # Check we have all the files we need. If we don't, it's likely because
        # there are relation updates in progress.
        for _, location in self._concourse_key_locations.items():
            if not os.path.exists(location):
                self.unit.status = BlockedStatus("Expected file doesn't exist yet: {}".format(location))
                event.defer()
                return
        container = self.unit.get_container("concourse-web")
        layer = self._concourse_layer()
        try:
            services = container.get_plan().to_dict().get("services", {})
        except ConnectionError:
            logger.info("Unable to connect to Pebble, deferring event")
            event.defer()
            return
        # Update our ingress definition if appropriate.
        self.ingress.update_config(self.ingress_config)
        if self._restart_required or services != layer["services"]:
            container.add_layer("concourse-web", layer, combine=True)
            logger.info("Added updated layer to concourse")
            if container.get_service("concourse-web").is_running():
                container.stop("concourse-web")
            container.start("concourse-web")
            self._restart_required = False
            logger.info("Restarted concourse-web service")
        self.unit.status = ActiveStatus()

        # XXX: Todo: set workload version
        # version = /usr/local/concourse/bin/concourse --version
        # self.unit.set_workload_version(version)

    def _concourse_layer(self):
        return {
            "services": {
                "concourse-web": {
                    "override": "replace",
                    "summary": "concourse web node",
                    "command": "/usr/local/bin/entrypoint.sh web",
                    "startup": "enabled",
                    "environment": self._env_config,
                }
            },
        }

    @property
    def _env_config(self):
        env_config = {
            "CONCOURSE_EXTERNAL_URL": "http://{}".format(self._external_url),
            "CONCOURSE_POSTGRES_HOST": self._stored.db_host,
            "CONCOURSE_POSTGRES_PORT": self._stored.db_port,
            "CONCOURSE_POSTGRES_DATABASE": self._stored.db_name,
            "CONCOURSE_POSTGRES_USER": self._stored.db_user,
            "CONCOURSE_POSTGRES_PASSWORD": self._stored.db_password,
            "CONCOURSE_ADD_LOCAL_USER": "myuser:mypass",
            "CONCOURSE_MAIN_TEAM_LOCAL_USER": "myuser",
        }
        env_config.update(self._concourse_key_locations)
        return env_config

    @property
    def _external_url(self):
        return self.config.get("external-url") or self.app.name

    @property
    def ingress_config(self):
        ingress_config = {
            "service-hostname": self._external_url,
            "service-name": self.app.name,
            "service-port": 8080,
            "tls-secret-name": self.config["tls-secret-name"],
        }
        return ingress_config


if __name__ == "__main__":
    main(ConcourseWebOperatorCharm, use_juju_for_storage=True)
