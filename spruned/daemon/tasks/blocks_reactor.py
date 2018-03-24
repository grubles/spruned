import asyncio
from spruned.daemon import exceptions
from spruned.application.logging_factory import Logger
from spruned.application.tools import async_delayed_task
from spruned.daemon.p2p import P2PInterface
from spruned.repositories.repository import Repository


class BlocksReactor:
    """
    This reactor keeps non-pruned blocks aligned to the best height.
    """
    def __init__(
            self,
            repository: Repository,
            interface: P2PInterface,
            loop=asyncio.get_event_loop(),
            prune=200,
            delayed_task=async_delayed_task
    ):
        self.repo = repository
        self.interface = interface
        self.loop = loop or asyncio.get_event_loop()
        self.lock = asyncio.Lock()
        self.delayer = delayed_task
        self._last_processed_block = None
        self._prune = prune
        self._max_per_batch = 10
        self._available = False
        self._fallback_check_interval = 30

    def set_last_processed_block(self, last):
        if last != self._last_processed_block:
            self._last_processed_block = last
            Logger.p2p.info(
                'Last processed block: %s (%s)',
                self._last_processed_block and self._last_processed_block['block_height'],
                self._last_processed_block and self._last_processed_block['block_hash'],
            )

    def on_header(self, best_header):
        Logger.p2p.debug('BlocksReactor.on_header: %s', best_header)
        self.loop.create_task(self._check_blockchain(best_header))

    async def check(self):
        try:
            best_header = self.repo.headers.get_best_header()
            await self._check_blockchain(best_header)
            self.loop.create_task(self._fallback_check_interval)
        except Exception as e:
            Logger.p2p.error('Error on BlocksReactor fallback %s', str(e))

    async def _check_blockchain(self, best_header):
        try:
            await self.lock.acquire()
            if best_header['block_height'] > self._last_processed_block['block_height']:
                self._on_blocks_behind_headers(best_header)
            elif not self._last_processed_block:
                self._on_blocks_behind_headers(best_header)
            elif best_header['block_height'] < self._last_processed_block['block_height']:
                self._on_headers_behind_blocks(best_header)
            else:
                if best_header['block_hash'] != self._last_processed_block['block_hash']:
                    raise exceptions.BlocksInconsistencyException
        except (
            exceptions.BlocksInconsistencyException
        ):
            Logger.p2p.exception('Exception checkping the blockchain')
            return
        finally:
            self.lock.release()

    async def _on_blocks_behind_headers(self, best_header):
        if self._last_processed_block:
            start = self._last_processed_block['block_hash']
        else:
            _bestheight = best_header['block_height'] - self._prune
            _startheight = _bestheight >= 0 and _bestheight or 0
            start = self.repo.headers.get_header_at_height(_startheight)

        blocks = await self.interface.get_blocks(start, best_header['block_hash'], self._max_per_batch)
        try:
            self.repo.headers.get_headers(*[block['block_hash'] for block in blocks])
        except:
            Logger.p2p.exception('Error fetching headers for downloaded blocks')
            raise exceptions.BlocksInconsistencyException
        try:
            saved_block = self.repo.blockchain.save_blocks(*blocks)
            Logger.p2p.debug('Saved block %s', saved_block)
        except:
            Logger.p2p.exception('Error saving blocks %s', blocks)
        return

    async def _on_headers_behind_blocks(self, best_header):
        try:
            self.repo.blockchain.get_block(best_header['blockhash'])
        except:
            Logger.p2p.exception('Error fetching block in headers_behind_blocks behaviour: %s', best_header)
            raise exceptions.BlocksInconsistencyException

    async def on_connected(self):
        self._available = True
        #self.loop.create_task(self.check())

    async def start(self):
        self.interface.add_on_connect_callback(self.on_connected)
        self.loop.create_task(self.interface.start())