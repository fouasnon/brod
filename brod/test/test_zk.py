"""
This runs tests for brod's ZooKeeper Producer and Consumer. It actually starts
an instance of ZooKeeper and multiple instances of Kafka. Since it's meant to 
run during development, it runs these servers on different ports and different
locations from the application defaults, to avoid conflicts.

Current assumptions this script makes:

1. ZooKeeper will be run on port 2182 (2181 is standard)
2. Kafka will run on ports 9101 and up, depending on the number of instances.
   (9092 is the usual port.)
3. You have Kafka installed and Kafka's /bin directory is in your PATH. In 
   particular, the tests require kafka-run-class.sh
4. Kafka will use the JMX ports from 10000 and up, depending on the number of
   instances. This is not something we can set in the config file -- we have
   to pass it in the form of an environment var JMX_PORT.
5. You have a /tmp directory

What you need to run the tests:

Note that the test order matters. Bringing ZooKeeper and the Kafka brokers up
takes a few seconds each time, so we try to avoid it when possible. The tests
are run in the order that they appear in the module, and at any time, we can
change the Kafka server topology by using nosetest's @with_setup decorator and
passing it a ServerTopology object. By convention, the test functions are named
after the topology that they're using. For example:

topology_003 = ServerTopology("003", 3, 5) # 3 brokers, 5 partitions each

@with_setup(setup_servers(topology_003)) 
def test_003_something():
    ...

def test_003_something_else():
    ...

topology_004 = ServerTopology("004", 1, 1) # 1 broker, 1 partition

@with_setup(setup_servers(topology_004)) 
def test_004_something():
    ...

def test_004_something_else():
    ...

If you have a test that you want to run with multiple topologies:

1. Make the real test function a parameterized one that doesn't start or end
   with "test" (so that nose doesn't automatically pick it up).
2. Make a test in the appropriate server topology grouping that just calls the
   real test with the appropriate arguments.

Finally: Try to use different consumer group names if at all possible, so that
the tests with the same topology setup won't interfere with each other.

TODO/FIXME:
1. We could save time at the expense of memory if we're willing to spin up all
   the servers for our various configurations at once (or at least a decent 
   group of them). But we'd have to refactor how we do the tracking, since 
   right now all the run start/stop stuff is in static vars of RunConfig

2. There's a whole lot of noise in the logs that comes out when you shut down
   a ZooKeeper instance and the underlying ZooKeeper library freaks out about
   the lost connections for the various notification subscriptions it's made.
   I tried forcing garbage collection and increasing the wait time after the
   servers are torn down, but something in the ZooKeeper library isn't getting
   deleted properly when things go out of scope. The upshot is that devs might
   get lots of superflous ZooKeeper errors in their logs.
"""
import logging
import os
import os.path
import signal
import subprocess
import time
from collections import namedtuple
from datetime import datetime
from functools import partial
from itertools import chain
from subprocess import Popen
from unittest import TestCase

from nose.tools import *

from zc.zk import ZooKeeper

from brod import Kafka
from brod.zk import ZKConsumer, ZKProducer

class ServerTopology(namedtuple('ServerTopology',
                                'name num_brokers partitions_per_broker')):
    @property
    def total_partitions(self):
        return self.num_brokers * self.partitions_per_broker

ZKConfig = namedtuple('ZKConfig', 'config_file data_dir client_port')
KafkaConfig = namedtuple('KafkaConfig', 
                         'config_file broker_id port log_dir ' + \
                         'num_partitions zk_server jmx_port')

KAFKA_BASE_PORT = 9101
JMX_BASE_PORT = 10000
ZK_PORT = 2182
ZK_CONNECT_STR = "localhost:{0}".format(ZK_PORT)

log = logging.getLogger("brod")


class RunConfig(object):
    """This container class just has a bunch of class level vars that are 
    manipulated by each setup_servers()/teardown() call. At any given point, it
    has the configuration state used by the current run of ZooKeeper + Kafka.

    Don't directly reset these values yourself. If you need a new configuration
    for a set of tests, give your test the decorator:
        group_001 = ServerTopology("001", 3, 5)
        @with_setup(setup_servers(group_001))
        def test_something():
            # do stuff here
    """
    kafka_configs = None
    kafka_processes = None
    run_dir = None
    zk_config = None
    zk_process = None

    @classmethod
    def clear(cls):
        cls.kafka_configs = cls.kafka_processes = cls.run_dir = cls.zk_config \
                          = cls.zk_process = None
    @classmethod
    def is_running(cls):
        return any([cls.kafka_configs, cls.kafka_processes, cls.run_dir, 
                    cls.zk_config, cls.zk_process])


def setup_servers(topology):
    def run_setup():
        # For those tests that ask for new server instances -- kill the old one.
        if RunConfig.is_running():
            teardown()

        timestamp = datetime.now().strftime('%Y-%m-%d-%H_%M_%s_%f')
        RunConfig.run_dir = os.path.join("/tmp", "brod_zk_test", timestamp)
        os.makedirs(RunConfig.run_dir)
        log.info("SETUP: Running with toplogy {0}".format(topology))
        log.info(("SETUP: {0.num_brokers} brokers, {0.partitions_per_broker} " +
                  "partitions per broker.").format(topology))
        log.info("SETUP: ZooKeeper and Kafka data in {0}".format(RunConfig.run_dir))

        # Set up configuration and data directories for ZK and Kafka
        RunConfig.zk_config = setup_zookeeper()
        RunConfig.kafka_configs = setup_kafka(topology.num_brokers, 
                                              topology.partitions_per_broker)

        # Start ZooKeeper...
        log.info("SETUP: Starting ZooKeeper with config {0}"
                 .format(RunConfig.zk_config))
        RunConfig.zk_process = Popen(["kafka-run-class.sh",
                                       "org.apache.zookeeper.server.quorum.QuorumPeerMain",
                                       RunConfig.zk_config.config_file],
                                      stdout=open(RunConfig.run_dir + "/zookeeper.log", "wb"),
                                      stderr=open(RunConfig.run_dir + "/zookeeper_error.log", "wb"),
                                      shell=False,
                                      preexec_fn=os.setsid)
        # Give ZK a little time to finish starting up before we start spawning
        # Kafka instances to connect to it.
        time.sleep(2)

        # Start Kafka. We use kafka-run-class.sh instead of 
        # kafka-server-start.sh because the latter sets the JMX_PORT to 9999
        # and we want to set it differently for each Kafka instance
        RunConfig.kafka_processes = []
        for kafka_config in RunConfig.kafka_configs:
            env = os.environ.copy()
            env["JMX_PORT"] = str(kafka_config.jmx_port)
            log.info("SETUP: Starting Kafka with config {0}".format(kafka_config))
            run_log = "kafka_{0}.log".format(kafka_config.broker_id)
            run_errs = "kafka_error_{0}.log".format(kafka_config.broker_id)
            process = Popen(["kafka-run-class.sh",
                             "kafka.Kafka", 
                             kafka_config.config_file],
                             stdout=open("{0}/{1}".format(RunConfig.run_dir, run_log), "wb"),
                             stderr=open("{0}/{1}".format(RunConfig.run_dir, run_errs), "wb"),
                             shell=False,
                             preexec_fn=os.setsid,
                             env=env)
            RunConfig.kafka_processes.append(process)
        
        # Now give the Kafka instances a little time to spin up...
        time.sleep(2)
    
    return run_setup

def setup_zookeeper():
    # Create all the directories we need...
    config_dir, data_dir = create_run_dirs("zookeeper/config", "zookeeper/data")
    # Write this session's config file...
    config_file = os.path.join(config_dir, "zookeeper.properties")
    zk_config = ZKConfig(config_file, data_dir, ZK_PORT)
    write_config("zookeeper.properties", config_file, zk_config)

    return zk_config

def setup_kafka(num_instances, num_partitions):
    config_dir, data_dir = create_run_dirs("kafka/config", "kafka/data")

    # Write this session's config file...
    configs = []
    for i in range(num_instances):
        config_file = os.path.join(config_dir, 
                                   "kafka.{0}.properties".format(i))
        log_dir = os.path.join(data_dir, str(i))
        os.makedirs(log_dir)
        kafka_config = KafkaConfig(config_file=config_file,
                                   broker_id=i,
                                   port=KAFKA_BASE_PORT + i,
                                   log_dir=log_dir,
                                   num_partitions=num_partitions,
                                   zk_server="localhost:{0}".format(ZK_PORT),
                                   jmx_port=JMX_BASE_PORT + i)
        configs.append(kafka_config)
        write_config("kafka.properties", config_file, kafka_config)

    return configs

def teardown():
    # Have to kill Kafka before ZooKeeper, or Kafka will get very distraught
    # You can't kill the processes with Popen.terminate() because what we
    # call is just a shell script that spawns off a Java process. But since
    # we did that bit with preexec_fn=os.setsid when we created them, we can
    # kill the entire process group with os.killpg
    if not RunConfig.is_running():
        return

    for process in RunConfig.kafka_processes:
        log.info("TEARDOWN: Terminating Kafka process {0}".format(process))
        os.killpg(process.pid, signal.SIGTERM)

    log.info("TEARDOWN: Terminating ZooKeeper process {0}"
             .format(RunConfig.zk_process))
    os.killpg(RunConfig.zk_process.pid, signal.SIGTERM)
    time.sleep(1)
    RunConfig.clear()

def terminate_process(process):
    os.killpg(process.pid, signal.SIGTERM)

def write_config(template_name, finished_location, format_obj):
    with open(template(template_name)) as template_file:
        template_text = template_file.read()
        config_text = template_text.format(format_obj)
        with open(finished_location, "wb") as finished_file:
            finished_file.write(config_text)

def create_run_dirs(*dirs):
    paths = [os.path.join(RunConfig.run_dir, d) for d in dirs]
    for path in paths:
        os.makedirs(path)
    return paths

def template(config):
    """Return the template configuration file for a given config file."""
    script_dir = os.path.dirname(os.path.realpath(__file__))
    return os.path.join(script_dir, "server_config", config)

####

def print_zk_snapshot():
    # Dump all the ZooKeeper state at this point
    zk = ZooKeeper(ZK_CONNECT_STR)
    print zk.export_tree(ephemeral=True)

################################ TESTS BEGIN ###################################

topology_001 = ServerTopology("001", 3, 5) # 3 brokers, 5 partitions each

@with_setup(setup_servers(topology_001)) 
def test_001_consumer_rebalancing():
    """Consumer rebalancing, with auto rebalancing."""
    for kafka_config in RunConfig.kafka_configs:
       k = Kafka("localhost", kafka_config.port)
       for topic in ["t1", "t2", "t3"]:
          k.produce(topic, ["bootstrap"], 0)
          time.sleep(1)

    producer = ZKProducer(ZK_CONNECT_STR, "t1")
    assert_equals(len(producer.broker_partitions), topology_001.total_partitions,
                  "We should be sending to all broker_partitions.")
           
    c1 = ZKConsumer(ZK_CONNECT_STR, "group_001", "t1")
    assert_equals(len(c1.broker_partitions), topology_001.total_partitions,
                  "Only one consumer, it should have all partitions.")
    c2 = ZKConsumer(ZK_CONNECT_STR, "group_001", "t1")
    assert_equals(len(c2.broker_partitions), (topology_001.total_partitions) / 2)

    time.sleep(1)
    assert_equals(len(set(c1.broker_partitions + c2.broker_partitions)),
                  topology_001.total_partitions,
                  "We should have all broker partitions covered.")

    c3 = ZKConsumer(ZK_CONNECT_STR, "group_001", "t1")
    assert_equals(len(c3.broker_partitions), (topology_001.total_partitions) / 3)

    time.sleep(1)
    assert_equals(sum(len(c.broker_partitions) for c in [c1, c2, c3]),
                  topology_001.total_partitions,
                  "All BrokerPartitions should be accounted for.")
    assert_equals(len(set(c1.broker_partitions + c2.broker_partitions + 
                          c3.broker_partitions)),
                  topology_001.total_partitions,
                  "There should be no overlaps")

def test_001_consumers():
    """Multi-broker/partition fetches"""
    c1 = ZKConsumer(ZK_CONNECT_STR, "group_002_consumers", "topic_001_consumers")
    
    result = c1.fetch()
    assert_equals(len(result), 0, "This shouldn't error, but it should be empty")

    for kafka_config in RunConfig.kafka_configs:
        k = Kafka("localhost", kafka_config.port)
        for partition in range(topology_001.partitions_per_broker):
            k.produce("topic_001_consumers", ["hello"], partition)
    time.sleep(2)

    # This should grab "hello" from every partition and every topic
    # c1.rebalance()
    result = c1.fetch()

    assert_equals(len(set(result.broker_partitions)), topology_001.total_partitions)
    for msg_set in result:
        assert_equals(msg_set.messages, ["hello"])

def test_001_broker_failure_no_rebalancing():
    """Test recovery from failed brokers"""





