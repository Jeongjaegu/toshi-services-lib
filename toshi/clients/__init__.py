try:
    from toshi.clients.ethereum_service_client import EthereumServiceClient
except ModuleNotFoundError as ex:
    if ex.name == 'ethereum':
        class EthereumServiceClient:
            def __init__(self, *args, **kwargs):
                raise Exception("Missing optional ethereum module, install with pip install toshi-services[ethereum]")
    else:
        raise
from toshi.clients.id_service_client import IdServiceClient
