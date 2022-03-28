import asyncio
import hashlib
import logging
import time

from grpc import (
    Server,
    ServicerContext,
)
from izihawa_utils.common import filter_none
from izihawa_utils.pb_to_json import MessageToDict
from library.aiogrpctools.base import aiogrpc_request_wrapper
from library.telegram.base import RequestContext
from library.telegram.utils import safe_execution
from nexus.hub.fancy_names import get_fancy_name
from nexus.hub.proto.delivery_service_pb2 import \
    StartDeliveryRequest as StartDeliveryRequestPb
from nexus.hub.proto.delivery_service_pb2 import \
    StartDeliveryResponse as StartDeliveryResponsePb
from nexus.hub.proto.delivery_service_pb2_grpc import (
    DeliveryServicer,
    add_DeliveryServicer_to_server,
)
from nexus.hub.user_manager import UserManager
from nexus.models.proto.operation_pb2 import \
    DocumentOperation as DocumentOperationPb
from nexus.models.proto.operation_pb2 import \
    StoreTelegramFileId as StoreTelegramFileIdPb
from nexus.models.proto.operation_pb2 import UpdateDocument as UpdateDocumentPb
from nexus.models.proto.typed_document_pb2 import \
    TypedDocument as TypedDocumentPb
from nexus.pylon.client import PylonClient
from nexus.pylon.exceptions import DownloadError
from nexus.pylon.proto.file_pb2 import FileResponse as FileResponsePb
from nexus.translations import t
from nexus.views.telegram import parse_typed_document_to_view
from nexus.views.telegram.common import close_button
from nexus.views.telegram.progress_bar import (
    ProgressBar,
    ProgressBarLostMessageError,
)
from prometheus_client import Gauge

from .base import (
    BaseHubService,
    is_group_or_channel,
)

downloads_gauge = Gauge('downloads_total', documentation='Currently downloading files')


async def operation_log(document_operation_pb):
    logging.getLogger('operation').info(msg=MessageToDict(document_operation_pb, preserving_proto_field_name=True))


class DownloadTask:
    def __init__(
        self,
        delivery_service,
        request_context,
        document_view,
        session_id: str,
    ):
        self.delivery_service = delivery_service
        self.request_context = request_context
        self.document_view = document_view
        self.session_id = session_id
        self.task = None

    async def schedule(self):
        self.task = asyncio.create_task(
            self.download_task(
                request_context=self.request_context,
                document_view=self.document_view,
            )
        )

        self.delivery_service.user_manager.add_task(self.request_context.chat.chat_id, self.document_view.id)
        self.delivery_service.downloadings.add(self)

        self.task.add_done_callback(self.done_callback)

    def done_callback(self, f):
        self.delivery_service.downloadings.remove(self)
        self.delivery_service.user_manager.remove_task(
            self.request_context.chat.chat_id,
            self.document_view.id,
        )

    async def download_task(self, request_context: RequestContext, document_view):
        throttle_secs = 2.0

        async def _on_fail():
            await self.delivery_service.telegram_clients[request_context.bot_name].send_message(
                request_context.chat.chat_id,
                t('MAINTENANCE', language=request_context.chat.language).format(
                    maintenance_picture_url=self.delivery_service.maintenance_picture_url
                ),
                buttons=[close_button()]
            )
        async with safe_execution(
            request_context=request_context,
            on_fail=_on_fail,
        ):
            progress_bar_download = ProgressBar(
                telegram_client=self.delivery_service.telegram_clients[request_context.bot_name],
                request_context=request_context,
                banner=t("LOOKING_AT", language=request_context.chat.language),
                header=f'⬇️ {document_view.get_filename()}',
                tail_text=t('TRANSMITTED_FROM', language=request_context.chat.language),
                throttle_secs=throttle_secs,
            )
            downloads_gauge.inc()
            start_time = time.time()
            try:
                file = await self.download(
                    document_view=document_view,
                    progress_bar=progress_bar_download,
                )
                if not file:
                    request_context.statbox(
                        action='missed',
                        duration=time.time() - start_time,
                        document_id=document_view.id,
                        index_alias=document_view.index_alias,
                    )
                    is_served_from_sharience = False
                    if self.delivery_service.is_sharience_enabled:
                        is_served_from_sharience = await self.try_sharience(
                            request_context=request_context,
                            document_view=document_view,
                        )
                    if not is_served_from_sharience:
                        request_context.statbox(
                            action='not_found',
                            document_id=document_view.id,
                            duration=time.time() - start_time,
                            index_alias=document_view.index_alias,
                        )
                        await self.respond_not_found(
                            request_context=request_context,
                            document_view=document_view,
                        )
                    return
                else:
                    request_context.statbox(
                        action='downloaded',
                        duration=time.time() - start_time,
                        document_id=document_view.id,
                        len=len(file),
                        index_alias=document_view.index_alias,
                    )

                progress_bar_upload = ProgressBar(
                    telegram_client=self.delivery_service.telegram_clients[request_context.bot_name],
                    request_context=request_context,
                    message=progress_bar_download.message,
                    banner=t("LOOKING_AT", language=request_context.chat.language),
                    header=f'⬇️ {document_view.get_filename()}',
                    tail_text=t('UPLOADED_TO_TELEGRAM', language=request_context.chat.language),
                    throttle_secs=throttle_secs
                )
                uploaded_message = await self.delivery_service.send_file(
                    document_view=self.document_view,
                    file=file,
                    progress_callback=progress_bar_upload.callback,
                    request_context=self.request_context,
                    session_id=self.session_id,
                    voting=not is_group_or_channel(self.request_context.chat.chat_id),
                )
                request_context.statbox(
                    action='uploaded',
                    duration=time.time() - start_time,
                    document_id=document_view.id,
                    index_alias=document_view.index_alias,
                )
                if self.delivery_service.should_store_hashes:
                    asyncio.create_task(self.store_hashes(
                        bot_name=request_context.bot_name,
                        document_view=document_view,
                        telegram_file_id=uploaded_message.file.id,
                        file=file,
                    ))
            except DownloadError:
                await self.external_cancel()
            except ProgressBarLostMessageError:
                self.request_context.statbox(
                    action='user_canceled',
                    duration=time.time() - start_time,
                    document_id=document_view.id,
                    index_alias=document_view.index_alias,
                )
            except asyncio.CancelledError:
                pass
            finally:
                downloads_gauge.dec()
                messages = filter_none([progress_bar_download.message])
                await (
                    self.delivery_service
                        .telegram_clients[request_context.bot_name]
                        .delete_messages(request_context.chat.chat_id, messages)
                )

    async def process_resp(self, resp, progress_bar, collected, filesize):
        progress_bar.set_source(get_fancy_name(resp.source))
        if resp.HasField('status'):
            if resp.status == FileResponsePb.Status.RESOLVING:
                await progress_bar.show_banner()
            if resp.status == FileResponsePb.Status.BEGIN_TRANSMISSION:
                collected.clear()
        elif resp.HasField('chunk'):
            collected.extend(resp.chunk.content)
            await progress_bar.callback(len(collected), filesize)

    async def respond_not_found(self, request_context: RequestContext, document_view):
        return await self.delivery_service.telegram_clients[request_context.bot_name].send_message(
            request_context.chat.chat_id,
            t("SOURCES_UNAVAILABLE", language=request_context.chat.language).format(
                document=document_view.get_robust_title()
            ),
            buttons=[close_button()]
        )

    async def try_sharience(self, request_context, document_view):
        if document_view.doi:
            request_context.statbox(action='try_sharience', doi=document_view.doi, index_alias=document_view.index_alias)
            pg_data = self.delivery_service.pool_holder.iterate(
                '''
                select sh.id, t.telegram_file_id as vote_sum
                from sharience as sh
                left join votes as v
                on sh.id = v.document_id
                left join telegram_files as t
                on sh.id = t.document_id
                where t.bot_name = %s
                group by sh.id, t.telegram_file_id
                having coalesce(sum(v.value), 0) > -1
                and sh.parent_id = %s
                order by coalesce(sum(v.value), 0) desc;
                ''', (self.request_context.bot_name, document_view.id,))
            async for document_id, telegram_file_id in pg_data:
                return await self.delivery_service.send_file(
                    document_id=document_id,
                    document_view=self.document_view,
                    file=telegram_file_id,
                    request_context=self.request_context,
                    session_id=self.session_id,
                    voting=True,
                )

    async def download(self, document_view, progress_bar):
        collected = bytearray()
        if document_view.doi:
            try:
                async for resp in self.delivery_service.pylon_client.by_doi(
                    doi=document_view.doi,
                    md5=document_view.md5,
                    error_log_func=self.request_context.error_log,
                ):
                    await self.process_resp(
                        resp=resp,
                        progress_bar=progress_bar,
                        collected=collected,
                        filesize=document_view.filesize,
                    )
                return bytes(collected)
            except DownloadError:
                pass
        if document_view.md5:
            try:
                async for resp in self.delivery_service.pylon_client.by_md5(
                    md5=document_view.md5,
                    error_log_func=self.request_context.error_log,
                ):
                    await self.process_resp(
                        resp=resp,
                        progress_bar=progress_bar,
                        collected=collected,
                        filesize=document_view.filesize,
                    )
                return bytes(collected)
            except DownloadError:
                pass

    async def external_cancel(self):
        self.task.cancel()
        self.request_context.statbox(
            action='externally_canceled',
            document_id=self.document_view.id,
            index=self.document_view.index,
        )
        await self.delivery_service.telegram_clients[self.request_context.bot_name].send_message(
            self.request_context.chat.chat_id,
            t("DOWNLOAD_CANCELED", language=self.request_context.chat.language).format(
                document=self.document_view.get_robust_title()
            ),
            buttons=[close_button()]
        )

    async def store_hashes(self, bot_name, document_view, telegram_file_id, file):
        document_pb = document_view.document_pb
        document_pb.filesize = len(file)
        if not document_pb.md5:
            document_pb.md5 = hashlib.md5(file).hexdigest()
        del document_pb.ipfs_multihashes[:]
        document_pb.ipfs_multihashes.extend(await self.delivery_service.get_ipfs_hashes(file=file))

        update_document_operation_pb = DocumentOperationPb(
            update_document=UpdateDocumentPb(
                fields=['filesize', 'ipfs_multihashes', 'md5'],
                typed_document=TypedDocumentPb(**{document_view.index_alias: document_pb}),
            ),
        )
        store_telegram_file_id_operation_pb = DocumentOperationPb(
            store_telegram_file_id=StoreTelegramFileIdPb(
                document_id=document_pb.id,
                telegram_file_id=telegram_file_id,
                bot_name=bot_name,
            ),
        )
        await asyncio.gather(
            operation_log(update_document_operation_pb),
            operation_log(store_telegram_file_id_operation_pb),
        )


class DeliveryService(DeliveryServicer, BaseHubService):
    def __init__(
        self,
        server: Server,
        service_name: str,
        ipfs_config: dict,
        is_sharience_enabled: bool,
        maintenance_picture_url: str,
        pool_holder,
        pylon_config: dict,
        should_store_hashes: bool,
        telegram_clients: dict,
        telegram_bot_configs: dict,
    ):
        super().__init__(
            service_name=service_name,
            ipfs_config=ipfs_config,
            telegram_clients=telegram_clients,
        )
        self.downloadings = set()
        self.is_sharience_enabled = is_sharience_enabled
        self.maintenance_picture_url = maintenance_picture_url
        self.pool_holder = pool_holder
        self.pylon_client = PylonClient(
            proxy=pylon_config['proxy'],
            resolve_proxy=pylon_config['resolve_proxy'],
        )
        self.server = server
        self.should_store_hashes = should_store_hashes
        self.telegram_bot_configs = telegram_bot_configs
        self.user_manager = UserManager()
        self.waits.extend([self.pylon_client])

    async def start(self):
        await super().start()
        add_DeliveryServicer_to_server(self, self.server)

    async def stop(self):
        for download in set(self.downloadings):
            await download.external_cancel()
        await asyncio.gather(*map(lambda x: x.task, self.downloadings))
        await self.ipfs_client.close()

    async def get_telegram_file_id(self, bot_name, document_id):
        pg_data = self.pool_holder.iterate('''
            select telegram_file_id from telegram_files where bot_name = %s and document_id = %s
        ''', (bot_name, document_id))
        async for telegram_file_id in pg_data:
            return telegram_file_id

    @aiogrpc_request_wrapper(log=False)
    async def start_delivery(
        self,
        request: StartDeliveryRequestPb,
        context: ServicerContext,
        metadata: dict,
    ) -> StartDeliveryResponsePb:
        request_context = RequestContext(
            bot_name=request.bot_name,
            chat=request.chat,
            request_id=metadata.get('request-id'),
        )
        request_context.add_default_fields(
            mode='delivery',
            session_id=metadata.get('session-id'),
            **self.get_default_service_fields(),
        )
        document_view = parse_typed_document_to_view(request.typed_document)
        telegram_file_id = None
        if self.telegram_bot_configs[request_context.bot_name]['should_use_telegram_file_id']:
            telegram_file_id = await self.get_telegram_file_id(request_context.bot_name, document_view.id)
        if telegram_file_id:
            try:
                await self.send_file(
                    document_view=document_view,
                    file=telegram_file_id,
                    session_id=metadata.get('session-id'),
                    request_context=request_context,
                    voting=not is_group_or_channel(request_context.chat.chat_id),
                )
                request_context.statbox(action='cache_hit', document_id=document_view.id, index_alias=document_view.index_alias)
            except ValueError:
                telegram_file_id = None
        if not telegram_file_id:
            if self.user_manager.has_task(request.chat.chat_id, document_view.id):
                return StartDeliveryResponsePb(status=StartDeliveryResponsePb.Status.ALREADY_DOWNLOADING)
            if self.user_manager.hit_limits(request.chat.chat_id):
                return StartDeliveryResponsePb(status=StartDeliveryResponsePb.Status.TOO_MANY_DOWNLOADS)
            await DownloadTask(
                delivery_service=self,
                document_view=document_view,
                request_context=request_context,
                session_id=metadata.get('session-id'),
            ).schedule()
        return StartDeliveryResponsePb(status=StartDeliveryResponsePb.Status.OK)
