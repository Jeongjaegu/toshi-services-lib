import asyncio
import binascii
import os
import signal
import subprocess
import urllib.request
import tornado.escape
import re
from toshi.config import config

from py_ecc.secp256k1 import privtopub
from ethereum.utils import encode_int32

from testing.common.database import (
    Database, DatabaseFactory, get_path_of, get_unused_port
)
from string import Template

from .faucet import FAUCET_PRIVATE_KEY, FAUCET_ADDRESS

from .ethminer import EthMiner

# https://github.com/ethcore/parity/wiki/Chain-specification
chaintemplate = Template("""{
    "name": "Dev",
    "engine": {
        "Ethash": {
            "params": {
                $extraEthashParams
                "minimumDifficulty": "$difficulty",
                "difficultyBoundDivisor": "0x0800",
                "durationLimit": "0x0a",
                "homesteadTransition": "0x0"
            }
        }
    },
    "params": {
        $extraParams
        "accountStartNonce": "0x0100000",
        "maximumExtraDataSize": "0x20",
        "minGasLimit": "0x1388",
        "networkID" : "0x42",
        "eip140Transition": "0x0",
        "eip211Transition": "0x0",
        "eip214Transition": "0x0",
        "eip658Transition": "0x0"
    },
    "genesis": {
        "seal": {
            "ethereum": {
                "nonce": "0x00006d6f7264656e",
                "mixHash": "0x00000000000000000000000000000000000000647572616c65787365646c6578"
            }
        },
        "difficulty": "$difficulty",
        "author": "0x0000000000000000000000000000000000000000",
        "timestamp": "0x00",
        "parentHash": "0x0000000000000000000000000000000000000000000000000000000000000000",
        "extraData": "0x",
        "gasLimit": "0x2fefd8"
    },
    "accounts": {
        "0000000000000000000000000000000000000001": { "balance": "1", "nonce": "1048576", "builtin": { "name": "ecrecover", "pricing": { "linear": { "base": 3000, "word": 0 } } } },
        "0000000000000000000000000000000000000002": { "balance": "1", "nonce": "1048576", "builtin": { "name": "sha256", "pricing": { "linear": { "base": 60, "word": 12 } } } },
        "0000000000000000000000000000000000000003": { "balance": "1", "nonce": "1048576", "builtin": { "name": "ripemd160", "pricing": { "linear": { "base": 600, "word": 120 } } } },
        "0000000000000000000000000000000000000004": { "balance": "1", "nonce": "1048576", "builtin": { "name": "identity", "pricing": { "linear": { "base": 15, "word": 3 } } } },
        "0000000000000000000000000000000000000005": { "builtin": { "name": "modexp", "activate_at": "0x0", "pricing": { "modexp": { "divisor": 20 } } } },
        "0000000000000000000000000000000000000006": { "builtin": { "name": "alt_bn128_add", "activate_at": "0x0", "pricing": { "linear": { "base": 500, "word": 0 } } } },
        "0000000000000000000000000000000000000007": { "builtin": { "name": "alt_bn128_mul", "activate_at": "0x0", "pricing": { "linear": { "base": 40000, "word": 0 } } } },
        "0000000000000000000000000000000000000008": { "builtin": { "name": "alt_bn128_pairing", "activate_at": "0x0", "pricing": { "alt_bn128_pairing": { "base": 100000, "pair": 80000 } } } },
        "$faucet": { "balance": "1606938044258990275541962092341162602522202993782792835301376", "nonce": "1048576" }
    }
}""")

def write_chain_file(version, fn, faucet, difficulty):

    if faucet.startswith('0x'):
        faucet = faucet[2:]

    if isinstance(difficulty, int):
        difficulty = hex(difficulty)
    elif isinstance(difficulty, str):
        if not difficulty.startswith("0x"):
            difficulty = "0x{}".format(difficulty)

    params = '"gasLimitBoundDivisor": "0x0400", "blockReward": "0x4563918244F40000",'
    if version < (1, 8, 0):
        extraEthashParams = params
        extraParams = ""
    else:
        extraEthashParams = ""
        extraParams = params

    with open(fn, 'w') as f:
        f.write(chaintemplate.substitute(faucet=faucet, difficulty=difficulty, extraParams=extraParams, extraEthashParams=extraEthashParams))

class ParityServer(Database):

    DEFAULT_SETTINGS = dict(auto_start=2,
                            base_dir=None,
                            parity_server=None,
                            author="0x0102030405060708090001020304050607080900",
                            faucet=FAUCET_ADDRESS,
                            port=None,
                            jsonrpc_port=None,
                            bootnodes=None,
                            node_key=None,
                            no_dapps=False,
                            dapps_port=None,
                            difficulty=None,
                            copy_data_from=None)

    subdirectories = ['data', 'tmp']

    def initialize(self):
        self.parity_server = self.settings.get('parity_server')
        if self.parity_server is None:
            self.parity_server = get_path_of('parity')

        p = subprocess.Popen([self.parity_server, '-v'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        outs, errs = p.communicate(timeout=15)

        for line in errs.split(b'\n') + outs.split(b'\n'):
            m = re.match("^\s+version\sParity\/v([0-9.]+).*$", line.decode('utf-8'))
            if m:
                v = tuple(int(i) for i in m.group(1).split('.'))
                break
        else:
            raise Exception("Unable to figure out Parity version")

        self.version = v
        self.chainfile = os.path.join(self.base_dir, 'chain.json')
        self.faucet = self.settings.get('faucet')

        self.author = self.settings.get('author')

        self.difficulty = self.settings.get('difficulty')
        if self.difficulty is None:
            self.difficulty = 1024

    def dsn(self, **kwargs):
        return {'node': 'enode://{}@127.0.0.1:{}'.format(self.public_key, self.settings['port']),
                'url': "http://localhost:{}/".format(self.settings['jsonrpc_port']),
                'network_id': "66"}

    def get_data_directory(self):
        return os.path.join(self.base_dir, 'data')

    def prestart(self):
        super(ParityServer, self).prestart()

        if self.settings['jsonrpc_port'] is None:
            self.settings['jsonrpc_port'] = get_unused_port()

        if self.version < (1, 7, 0) and self.settings['no_dapps'] is False and self.settings['dapps_port'] is None:
            self.settings['dapps_port'] = get_unused_port()

        if self.settings['node_key'] is None:
            self.settings['node_key'] = "{:0>64}".format(binascii.b2a_hex(os.urandom(32)).decode('ascii'))

        pub_x, pub_y = privtopub(binascii.a2b_hex(self.settings['node_key']))
        pub = encode_int32(pub_x) + encode_int32(pub_y)
        self.public_key = "{:0>128}".format(binascii.b2a_hex(pub).decode('ascii'))

        # write chain file
        write_chain_file(self.version, self.chainfile, self.faucet, self.difficulty)

    def get_server_commandline(self):
        if self.author.startswith("0x"):
            author = self.author[2:]
        else:
            author = self.author

        cmd = [self.parity_server,
               "--no-ui",
               "--port", str(self.settings['port']),
               "--datadir", self.get_data_directory(),
               "--no-color",
               "--chain", self.chainfile,
               "--author", author,
               "--tracing", 'on',
               "--node-key", self.settings['node_key']]

        # check version
        if self.version >= (1, 7, 0):
            cmd.extend(["--jsonrpc-port", str(self.settings['jsonrpc_port']),
                        "--jsonrpc-hosts", "all",
                        "--no-ws"])
        else:
            cmd.extend(["--rpcport", str(self.settings['jsonrpc_port'])])

        if self.settings['no_dapps']:
            cmd.extend(['--no-dapps'])
        elif self.version < (1, 7, 0):
            cmd.extend(['--dapps-port', str(self.settings['dapps_port'])])

        if self.settings['bootnodes'] is not None:
            if isinstance(self.settings['bootnodes'], list):
                self.settings['bootnodes'] = ','.join(self.settings['bootnodes'])

            cmd.extend(['--bootnodes', self.settings['bootnodes']])

        return cmd

    def is_server_available(self):
        try:
            urllib.request.urlopen(
                urllib.request.Request(
                    self.dsn()['url'],
                    headers={'Content-Type': "application/json"},
                    data=tornado.escape.json_encode({
                        "jsonrpc": "2.0",
                        "id": "1234",
                        "method": "eth_getBalance",
                        "params": ["0x{}".format(self.author), "latest"]
                    }).encode('utf-8')
                ))
            return True
        except Exception as e:
            if not hasattr(e, 'reason') or not isinstance(e.reason, ConnectionRefusedError):
                print(e)
            return False

    def pause(self):
        """stops service, without calling the cleanup"""
        self.terminate(signal.SIGTERM)


class ParityServerFactory(DatabaseFactory):
    target_class = ParityServer

def requires_parity(func=None, difficulty=None, pass_args=False, pass_parity=False, pass_ethminer=False, debug_ethminer=False):
    """Used to ensure all database connections are returned to the pool
    before finishing the test"""

    def wrap(fn):

        async def wrapper(self, *args, **kwargs):

            parity = ParityServer(difficulty=difficulty)
            ethminer = EthMiner(jsonrpc_url=parity.dsn()['url'],
                                debug=debug_ethminer)

            config['ethereum'] = parity.dsn()

            if pass_args:
                kwargs['parity'] = parity
                kwargs['ethminer'] = ethminer
            if pass_ethminer:
                if pass_ethminer is True:
                    kwargs['ethminer'] = ethminer
                else:
                    kwargs[pass_ethminer] = ethminer
            if pass_parity:
                if pass_parity is True:
                    kwargs['parity'] = parity
                else:
                    kwargs[pass_parity] = parity

            try:
                f = fn(self, *args, **kwargs)
                if asyncio.iscoroutine(f):
                    await f
            finally:
                # this is a hack for dealing with aiohttp "Unclosed client session" errors
                # since if we're done with the test we don't need the clients any more
                # so we can shut them all down
                try:
                    from toshi.jsonrpc.aiohttp_client import HTTPClient
                    for c in list(HTTPClient._async_clients().values()):
                        await c.close()
                except ModuleNotFoundError:
                    pass
                # no need for graceful shutdown, and waiting takes a long time, JUST KILL IT!
                ethminer.stop(_signal=signal.SIGKILL)
                parity.stop(_signal=signal.SIGKILL)
                del config['ethereum']

        return wrapper

    if func is not None:
        return wrap(func)
    else:
        return wrap
