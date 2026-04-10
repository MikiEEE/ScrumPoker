"""Client helpers built on top of the smallOS cooperative socket layer."""

from .SmallHTTP import HTTPProtocolError, SSEProtocolError, SmallHTTPClient, SmallHTTPResponse, SmallSSEClient
from .SmallMQTT import SmallMQTTClient
from .SmallRedis import SmallRedisClient
from .SmallStream import SmallStream, StreamClosedError
from .SmallWebSocket import SmallWebSocketClient, WebSocketProtocolError

__all__ = [
    "HTTPProtocolError",
    "SmallHTTPClient",
    "SmallHTTPResponse",
    "SmallMQTTClient",
    "SmallRedisClient",
    "SmallSSEClient",
    "SmallStream",
    "SmallWebSocketClient",
    "SSEProtocolError",
    "StreamClosedError",
    "WebSocketProtocolError",
]
