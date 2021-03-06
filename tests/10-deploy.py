#!/usr/bin/python3

import amulet
import os
import re
import unittest
import yaml
import subprocess

from tempfile import mkdtemp
from path import Path
from shutil import rmtree


class TestCharm(unittest.TestCase):

    @classmethod
    def setUpClass(self):

        # Get the relative bundle path from the environment variable.
        self.bundle = os.getenv('BUNDLE', 'bundle.yaml')
        # Create a path to the bundle based on this file's location.
        self.bundle_path = os.path.join(os.path.dirname(__file__),
                                        '..',
                                        self.bundle)
        # Normalize the path to the bundle.
        self.bundle_path = os.path.abspath(self.bundle_path)

        print('Deploying bundle: {0}'.format(self.bundle_path))

        self.deployment = amulet.Deployment()
        with open(self.bundle_path, 'r') as bundle_file:
            contents = yaml.safe_load(bundle_file)
            self.deployment.load(contents)

        self.deployment.setup(timeout=1200)
        self.deployment.sentry.wait()

        self.swarm = self.deployment.sentry['swarm']
        self.consul = self.deployment.sentry['consul']
        # Setup a temporary docker-swarm workspace
        self.sub = Path(mkdtemp('docker-credentials'))

    @classmethod
    def tearDownClass(self):
        rmtree(self.sub)

    def test_swarm_manager(self):
        # Are we running the manager?
        leader = get_leader(self.swarm)
        if not leader:
            raise "No leader found. Deployment broken"
        out = leader.run('docker ps')
        assert 'swarm_manager_1' in out[0]
        # under no circumstances should the containers
        # be cycling this early.
        assert 'restarting' not in out[0]

    def test_swarm_agents(self):
        ''' Cycle through every unit in the service and ensure the agent
            container is running '''
        for unit in self.swarm:
            out = unit.run('docker ps')
            assert 'swarm_agent_1' in out[0]
            assert 'restarting' not in out[0]

    def test_consul_storage_configuration(self):
        ''' Cycle through everys warm unit and ensure the daemon is using
            consul as a backend storage service for coordination of networking
            config '''

        consul_addresses = []
        for unit in self.consul:
            caddr = unit.relation('api', 'swarm:consul')['private-address']
            consul_addresses.append(caddr)

        for unit in self.swarm:
            out = unit.run('docker info')
            needle = re.search('consul://(.+?):8500', out[0])
            if needle:
                consul_address = needle.group(1)
                assert consul_address in consul_addresses
            else:
                raise Exception("Missing consul storage output on unit {}".format(unit.info['service']))  # noqa

    def test_tls_swarm_client_credentials(self):
        ''' The master unit generates swarm credentials. This test method
        will pull those credentials and run a container on the swarm cluster
        to validate '''

        # Setup some path variables to condense code
        unpack_path = Path('/home/ubuntu')
        archive_path = Path('/home/ubuntu/swarm_credentials.tar')
        ca_path = unpack_path + '/swarm_credentials/ca.pem'
        cert_path = unpack_path + '/swarm_credentials/cert.pem'
        key_path = unpack_path + '/swarm_credentials/key.pem'

        # Determine leader, and extract the credentials
        leader = get_leader(self.swarm)
        leader.run('mkdir -p {}'.format(unpack_path))
        out = leader.run('cd /home/ubuntu && tar xvf {}'.format(archive_path))
        if out[1]:
            raise "Error extracting credentials archive. Failing test"
        # Read the contents of the certificates
        ca_contents = leader.file_contents(ca_path)
        crt_contents = leader.file_contents(cert_path)
        key_contents = leader.file_contents(key_path)

        # Create a temporary path on disk, and setup the DOCKER_HOME
        with open(self.sub + '/ca.pem', 'w+') as fp:
            fp.write(ca_contents)
        with open(self.sub + '/cert.pem', 'w+') as fp:
            fp.write(crt_contents)
        with open(self.sub + '/key.pem', 'w+') as fp:
            fp.write(key_contents)

        os.environ['DOCKER_CERT_PATH'] = self.sub
        os.environ['DOCKER_TLS_VERIFY'] = '1'
        os.environ['DOCKER_HOST'] = 'tcp://{}:3376'.format(leader.info['public-address'])  # noqa

        # Have docker inspect the swarm cluster, and verify we have 2 nodes.
        # good enough of a full stack test to validate TLS is working against
        # the cluster.
        p = subprocess.Popen(
            ['docker', 'info'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = p.communicate()
        output = stdout if p.returncode == 0 else stderr
        out = output.decode('utf8')
        # Knowing we have the cluster on the wire will be good enough to
        # assert TLS is working.
        assert 'Nodes: 2' in out


# Helper method to determine which unit is a leader in a given set of sentries
def get_leader(charm):
    for unit in charm:
        out = unit.run('is-leader')
        if out[0] == 'True':
            return unit


if __name__ == "__main__":
    unittest.main()
