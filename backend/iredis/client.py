"""Process-wide Redis connection.

Always a cluster client. Every environment runs Redis cluster-enabled — prod on
its own droplet, local and dev as a single node holding all 16384 slots — so
there is no standalone path to keep working and no branch here. A single-node
cluster behaves identically to a standalone server for everything below; what it
buys is that the client which will meet a real multi-shard cluster is the one
running everywhere.

Every call site uses single-key commands or pub/sub, both of which are safe at
any shard count:

  - pub/sub is not key-routed. PUBLISH broadcasts across the cluster bus to
    every node, and a subscriber binds to whichever node owns the slot its
    channel name hashes to, so it still sees every message however slots are
    distributed.
  - the migration lock and the email-webhook dedupe key are single-key, so
    each routes to one node and none spans slots.

There are no multi-key commands anywhere — no MGET/MSET, no cross-key pipeline,
no SCAN/KEYS, no Lua beyond the script redis-py's own Lock registers. Keep it
that way: a multi-key command whose keys hash to different slots raises
CrossSlotError, and the fix for that is a hash tag in the key names, not a
change here.
"""

from __future__ import annotations

from redis.asyncio.cluster import RedisCluster

from settings import get_settings

_redis: RedisCluster | None = None

# Deadline on every socket read and on connect. Pinned here rather than left to
# redis-py, whose default differs by client class — the cluster client uses 5s
# where the standalone client uses None — so an unset value would depend on a
# choice made in a library, not in this file.
SOCKET_TIMEOUT_SECONDS = 5.0

# NOTE for anyone adding a blocking command (BLPOP and friends): the server-side
# wait must stay strictly under SOCKET_TIMEOUT_SECONDS, because `BLPOP key N`
# writes nothing until t=N and the reply needs room to travel and parse inside
# the read deadline. Nothing here blocks today — no queue in this codebase runs
# on Redis; work is distributed through `scheduled_jobs` and Kafka.


def get_redis() -> RedisCluster:
    global _redis
    if _redis is None:
        # The URL is a seed, not the whole cluster: the client discovers the
        # remaining nodes from it, so adding a shard needs no config change.
        # This node does have to be reachable at startup for that discovery.
        _redis = RedisCluster.from_url(
            get_settings().redis.url,
            decode_responses=True,
            socket_timeout=SOCKET_TIMEOUT_SECONDS,
            socket_connect_timeout=SOCKET_TIMEOUT_SECONDS,
        )
    return _redis
