from aries_cloudagent.multitenant.base import BaseMultitenantManager
import json

from asynctest import mock as async_mock, TestCase as AsyncTestCase

from .....cache.base import BaseCache
from .....cache.in_memory import InMemoryCache
from .....connections.models.conn_record import ConnRecord
from .....connections.models.connection_target import ConnectionTarget
from .....connections.models.diddoc import (
    DIDDoc,
    PublicKey,
    PublicKeyType,
    Service,
)
from .....core.in_memory import InMemoryProfile
from .....ledger.base import BaseLedger
from .....messaging.responder import BaseResponder, MockResponder
from .....messaging.decorators.attach_decorator import AttachDecorator
from .....multitenant.manager import MultitenantManager
from .....storage.error import StorageNotFoundError
from .....transport.inbound.receipt import MessageReceipt
from .....wallet.did_info import DIDInfo
from .....wallet.error import WalletError
from .....wallet.in_memory import InMemoryWallet
from .....wallet.did_method import DIDMethod
from .....wallet.key_type import KeyType
from .....did.did_key import DIDKey

from .....connections.base_manager import BaseConnectionManagerError

from ....coordinate_mediation.v1_0.manager import MediationManager
from ....coordinate_mediation.v1_0.messages.keylist_update import (
    KeylistUpdate,
    KeylistUpdateRule,
)
from ....coordinate_mediation.v1_0.models.mediation_record import MediationRecord
from ....didcomm_prefix import DIDCommPrefix
from ....out_of_band.v1_0.manager import OutOfBandManager
from ....out_of_band.v1_0.messages.invitation import HSProto, InvitationMessage
from ....out_of_band.v1_0.messages.service import Service as OOBService

from .. import manager as test_module
from ..manager import DIDXManager, DIDXManagerError


class TestConfig:

    test_seed = "testseed000000000000000000000001"
    test_did = "55GkHamhTU1ZbTbV2ab9DE"
    test_verkey = "3Dn1SJNPaCXcvvJvSbsFWP2xaCjMom3can8CQNhWrTRx"
    test_endpoint = "http://localhost"

    test_target_did = "GbuDUYXaUZRfHD2jeDuQuP"
    test_target_verkey = "9WCgWKUaAJj3VWxxtzvvMQN3AoFxoBtBDo9ntwJnVVCC"

    def make_did_doc(self, did, verkey):
        doc = DIDDoc(did=did)
        controller = did
        ident = "1"
        pk_value = verkey
        pk = PublicKey(
            did, ident, pk_value, PublicKeyType.ED25519_SIG_2018, controller, False
        )
        doc.set(pk)
        recip_keys = [pk]
        router_keys = []
        service = Service(
            did, "indy", "IndyAgent", recip_keys, router_keys, TestConfig.test_endpoint
        )
        doc.set(service)
        return doc


class TestDidExchangeManager(AsyncTestCase, TestConfig):
    async def setUp(self):
        self.responder = MockResponder()

        self.session = InMemoryProfile.test_session(
            {
                "default_endpoint": "http://aries.ca/endpoint",
                "default_label": "This guy",
                "additional_endpoints": ["http://aries.ca/another-endpoint"],
                "debug.auto_accept_invites": True,
                "debug.auto_accept_requests": True,
                "multitenant.enabled": True,
                "wallet.id": True,
            },
            bind={BaseResponder: self.responder, BaseCache: InMemoryCache()},
        )
        self.context = self.session.context

        self.did_info = await self.session.wallet.create_local_did(
            method=DIDMethod.SOV,
            key_type=KeyType.ED25519,
        )

        self.ledger = async_mock.create_autospec(BaseLedger)
        self.ledger.__aenter__ = async_mock.CoroutineMock(return_value=self.ledger)
        self.ledger.get_endpoint_for_did = async_mock.CoroutineMock(
            return_value=TestConfig.test_endpoint
        )
        self.session.context.injector.bind_instance(BaseLedger, self.ledger)

        self.multitenant_mgr = async_mock.MagicMock(MultitenantManager, autospec=True)
        self.session.context.injector.bind_instance(
            BaseMultitenantManager, self.multitenant_mgr
        )

        self.manager = DIDXManager(self.session)
        assert self.manager.session
        self.oob_manager = OutOfBandManager(self.session)
        self.test_mediator_routing_keys = [
            DIDKey.from_public_key_b58(
                "3Dn1SJNPaCXcvvJvSbsFWP2xaCjMom3can8CQNhWrTRR", KeyType.ED25519
            ).did
        ]
        self.test_mediator_conn_id = "mediator-conn-id"
        self.test_mediator_endpoint = "http://mediator.example.com"

    async def test_verify_diddoc(self):
        did_doc = self.make_did_doc(
            TestConfig.test_target_did,
            TestConfig.test_target_verkey,
        )
        did_doc_attach = AttachDecorator.data_base64(did_doc.serialize())
        with self.assertRaises(DIDXManagerError):
            await self.manager.verify_diddoc(self.session.wallet, did_doc_attach)

        await did_doc_attach.data.sign(self.did_info.verkey, self.session.wallet)

        await self.manager.verify_diddoc(self.session.wallet, did_doc_attach)

        did_doc_attach.data.base64_ = "YmFpdCBhbmQgc3dpdGNo"
        with self.assertRaises(DIDXManagerError):
            await self.manager.verify_diddoc(self.session.wallet, did_doc_attach)

    async def test_receive_invitation(self):
        self.session.context.update_settings({"public_invites": True})
        mediation_record = MediationRecord(
            role=MediationRecord.ROLE_CLIENT,
            state=MediationRecord.STATE_GRANTED,
            connection_id=self.test_mediator_conn_id,
            routing_keys=self.test_mediator_routing_keys,
            endpoint=self.test_mediator_endpoint,
        )
        await mediation_record.save(self.session)

        with async_mock.patch.object(
            test_module, "AttachDecorator", autospec=True
        ) as mock_attach_deco, async_mock.patch.object(
            self.multitenant_mgr, "get_default_mediator"
        ) as mock_get_default_mediator:
            mock_get_default_mediator.return_value = mediation_record
            invi_rec = await self.oob_manager.create_invitation(
                my_endpoint="testendpoint",
                hs_protos=[HSProto.RFC23],
            )
            invi_msg = invi_rec.invitation
            mock_attach_deco.data_base64 = async_mock.MagicMock(
                return_value=async_mock.MagicMock(
                    data=async_mock.MagicMock(sign=async_mock.CoroutineMock())
                )
            )
            invitee_record = await self.manager.receive_invitation(invi_msg)
            assert invitee_record.state == ConnRecord.State.REQUEST.rfc23

    async def test_receive_invitation_no_auto_accept(self):
        mediation_record = MediationRecord(
            role=MediationRecord.ROLE_CLIENT,
            state=MediationRecord.STATE_GRANTED,
            connection_id=self.test_mediator_conn_id,
            routing_keys=self.test_mediator_routing_keys,
            endpoint=self.test_mediator_endpoint,
        )
        await mediation_record.save(self.session)
        with async_mock.patch.object(
            self.multitenant_mgr, "get_default_mediator"
        ) as mock_get_default_mediator:
            mock_get_default_mediator.return_value = mediation_record
            invi_rec = await self.oob_manager.create_invitation(
                my_endpoint="testendpoint",
                hs_protos=[HSProto.RFC23],
            )

            invitee_record = await self.manager.receive_invitation(
                invi_rec.invitation,
                auto_accept=False,
            )
            assert invitee_record.state == ConnRecord.State.INVITATION.rfc23

    async def test_receive_invitation_bad_invitation(self):
        x_invites = [
            InvitationMessage(),
            InvitationMessage(services=[OOBService()]),
            InvitationMessage(
                services=[
                    OOBService(
                        recipient_keys=["3Dn1SJNPaCXcvvJvSbsFWP2xaCjMom3can8CQNhWrTRx"]
                    )
                ]
            ),
        ]

        for x_invite in x_invites:
            with self.assertRaises(DIDXManagerError):
                await self.manager.receive_invitation(x_invite)

    async def test_create_request_implicit(self):
        mediation_record = MediationRecord(
            role=MediationRecord.ROLE_CLIENT,
            state=MediationRecord.STATE_GRANTED,
            connection_id=self.test_mediator_conn_id,
            routing_keys=self.test_mediator_routing_keys,
            endpoint=self.test_mediator_endpoint,
        )
        await mediation_record.save(self.session)

        with async_mock.patch.object(
            self.manager, "create_did_document", async_mock.CoroutineMock()
        ) as mock_create_did_doc, async_mock.patch.object(
            self.multitenant_mgr, "get_default_mediator"
        ) as mock_get_default_mediator:
            mock_get_default_mediator.return_value = mediation_record
            mock_create_did_doc.return_value = async_mock.MagicMock(
                serialize=async_mock.MagicMock(return_value={})
            )
            conn_rec = await self.manager.create_request_implicit(
                their_public_did=TestConfig.test_target_did,
                my_label=None,
                my_endpoint=None,
                mediation_id=mediation_record._id,
            )

            assert conn_rec

    async def test_create_request_implicit_use_public_did(self):

        mediation_record = MediationRecord(
            role=MediationRecord.ROLE_CLIENT,
            state=MediationRecord.STATE_GRANTED,
            connection_id=self.test_mediator_conn_id,
            routing_keys=self.test_mediator_routing_keys,
            endpoint=self.test_mediator_endpoint,
        )
        await mediation_record.save(self.session)

        info_public = await self.session.wallet.create_public_did(
            DIDMethod.SOV,
            KeyType.ED25519,
        )
        conn_rec = await self.manager.create_request_implicit(
            their_public_did=TestConfig.test_target_did,
            my_label=None,
            my_endpoint=None,
            mediation_id=mediation_record._id,
            use_public_did=True,
        )

        assert info_public.did == conn_rec.my_did

    async def test_create_request_implicit_no_public_did(self):
        with self.assertRaises(WalletError) as context:
            await self.manager.create_request_implicit(
                their_public_did=TestConfig.test_target_did,
                my_label=None,
                my_endpoint=None,
                mediation_id=None,
                use_public_did=True,
            )

        assert "No public DID configured" in str(context.exception)

    async def test_create_request(self):
        mock_conn_rec = async_mock.MagicMock(
            connection_id="dummy",
            my_did=self.did_info.did,
            their_did=TestConfig.test_target_did,
            their_role=ConnRecord.Role.RESPONDER.rfc23,
            state=ConnRecord.State.REQUEST.rfc23,
            retrieve_invitation=async_mock.CoroutineMock(
                return_value=async_mock.MagicMock(
                    services=[TestConfig.test_target_did],
                )
            ),
            save=async_mock.CoroutineMock(),
        )

        with async_mock.patch.object(
            self.manager, "create_did_document", async_mock.CoroutineMock()
        ) as mock_create_did_doc:
            mock_create_did_doc.return_value = async_mock.MagicMock(
                serialize=async_mock.MagicMock(return_value={})
            )

            didx_req = await self.manager.create_request(mock_conn_rec)
            assert didx_req

    async def test_create_request_multitenant(self):
        self.context.update_settings(
            {"multitenant.enabled": True, "wallet.id": "test_wallet"}
        )

        with async_mock.patch.object(
            InMemoryWallet, "create_local_did", autospec=True
        ) as mock_wallet_create_local_did, async_mock.patch.object(
            self.manager, "create_did_document", async_mock.CoroutineMock()
        ) as mock_create_did_doc, async_mock.patch.object(
            test_module, "AttachDecorator", autospec=True
        ) as mock_attach_deco:
            mock_create_did_doc.return_value = async_mock.MagicMock(
                serialize=async_mock.MagicMock(return_value={})
            )
            mock_wallet_create_local_did.return_value = DIDInfo(
                TestConfig.test_did,
                TestConfig.test_verkey,
                None,
                method=DIDMethod.SOV,
                key_type=KeyType.ED25519,
            )
            mock_attach_deco.data_base64 = async_mock.MagicMock(
                return_value=async_mock.MagicMock(
                    data=async_mock.MagicMock(sign=async_mock.CoroutineMock())
                )
            )

            await self.manager.create_request(
                async_mock.MagicMock(
                    invitation_key=TestConfig.test_verkey,
                    their_label="Hello",
                    their_role=ConnRecord.Role.RESPONDER.rfc160,
                    alias="Bob",
                    my_did=None,
                    retrieve_invitation=async_mock.CoroutineMock(
                        return_value=async_mock.MagicMock(
                            services=[TestConfig.test_target_did],
                        )
                    ),
                    save=async_mock.CoroutineMock(),
                )
            )

            self.multitenant_mgr.add_key.assert_called_once_with(
                "test_wallet", TestConfig.test_verkey
            )

    async def test_create_request_mediation_id(self):
        mediation_record = MediationRecord(
            role=MediationRecord.ROLE_CLIENT,
            state=MediationRecord.STATE_GRANTED,
            connection_id=self.test_mediator_conn_id,
            routing_keys=self.test_mediator_routing_keys,
            endpoint=self.test_mediator_endpoint,
        )
        await mediation_record.save(self.session)

        invi = InvitationMessage(
            comment="test",
            handshake_protocols=[
                pfx.qualify(HSProto.RFC23.name) for pfx in DIDCommPrefix
            ],
            services=[TestConfig.test_did],
        )
        record = ConnRecord(
            invitation_key=TestConfig.test_verkey,
            invitation_msg_id=invi._id,
            their_label="Hello",
            their_role=ConnRecord.Role.RESPONDER.rfc160,
            alias="Bob",
            my_did=None,
        )

        await record.save(self.session)
        await record.attach_invitation(self.session, invi)

        with async_mock.patch.object(
            self.manager, "create_did_document", async_mock.CoroutineMock()
        ) as mock_create_did_doc:
            mock_create_did_doc.return_value = async_mock.MagicMock(
                serialize=async_mock.MagicMock(return_value={})
            )

            didx_req = await self.manager.create_request(
                record,
                my_endpoint="http://testendpoint.com/endpoint",
                mediation_id=mediation_record._id,
            )
            assert didx_req
        assert len(self.responder.messages) == 1
        message, used_kwargs = self.responder.messages[0]
        assert isinstance(message, KeylistUpdate)
        assert (
            "connection_id" in used_kwargs
            and used_kwargs["connection_id"] == self.test_mediator_conn_id
        )

    async def test_create_request_my_endpoint(self):
        mock_conn_rec = async_mock.MagicMock(
            connection_id="dummy",
            my_did=None,
            their_did=TestConfig.test_target_did,
            their_role=ConnRecord.Role.RESPONDER.rfc23,
            their_label="Bob",
            invitation_key=TestConfig.test_verkey,
            state=ConnRecord.State.REQUEST.rfc23,
            alias="Bob",
            retrieve_invitation=async_mock.CoroutineMock(
                return_value=async_mock.MagicMock(
                    services=[TestConfig.test_target_did],
                )
            ),
            save=async_mock.CoroutineMock(),
        )

        with async_mock.patch.object(
            self.manager, "create_did_document", async_mock.CoroutineMock()
        ) as mock_create_did_doc:
            mock_create_did_doc.return_value = async_mock.MagicMock(
                serialize=async_mock.MagicMock(return_value={})
            )

            didx_req = await self.manager.create_request(
                mock_conn_rec,
                my_endpoint="http://testendpoint.com/endpoint",
            )
            assert didx_req

    async def test_receive_request_explicit_public_did(self):
        mock_request = async_mock.MagicMock(
            did=TestConfig.test_did,
            did_doc_attach=async_mock.MagicMock(
                data=async_mock.MagicMock(
                    verify=async_mock.CoroutineMock(return_value=True),
                    signed=async_mock.MagicMock(
                        decode=async_mock.MagicMock(return_value="dummy-did-doc")
                    ),
                )
            ),
            _thread=async_mock.MagicMock(pthid="did:sov:publicdid0000000000000"),
        )

        mediation_record = MediationRecord(
            role=MediationRecord.ROLE_CLIENT,
            state=MediationRecord.STATE_GRANTED,
            connection_id=self.test_mediator_conn_id,
            routing_keys=self.test_mediator_routing_keys,
            endpoint=self.test_mediator_endpoint,
        )
        await mediation_record.save(self.session)

        await self.session.wallet.create_local_did(
            method=DIDMethod.SOV,
            key_type=KeyType.ED25519,
            seed=None,
            did=TestConfig.test_did,
        )

        STATE_REQUEST = ConnRecord.State.REQUEST
        self.session.context.update_settings({"public_invites": True})
        ACCEPT_AUTO = ConnRecord.ACCEPT_AUTO
        with async_mock.patch.object(
            test_module, "ConnRecord", async_mock.MagicMock()
        ) as mock_conn_rec_cls, async_mock.patch.object(
            test_module, "DIDDoc", autospec=True
        ) as mock_did_doc, async_mock.patch.object(
            test_module, "DIDPosture", autospec=True
        ) as mock_did_posture, async_mock.patch.object(
            test_module, "AttachDecorator", autospec=True
        ) as mock_attach_deco, async_mock.patch.object(
            test_module, "DIDXResponse", autospec=True
        ) as mock_response, async_mock.patch.object(
            self.manager, "create_did_document", async_mock.CoroutineMock()
        ) as mock_create_did_doc, async_mock.patch.object(
            MediationManager, "prepare_request", autospec=True
        ) as mock_mediation_mgr_prep_req:
            mock_create_did_doc.return_value = async_mock.MagicMock(
                serialize=async_mock.MagicMock(return_value={})
            )
            mock_mediation_mgr_prep_req.return_value = (mediation_record, mock_request)

            mock_conn_record = async_mock.MagicMock(
                accept=ACCEPT_AUTO,
                my_did=None,
                state=STATE_REQUEST.rfc23,
                attach_request=async_mock.CoroutineMock(),
                retrieve_request=async_mock.CoroutineMock(),
                metadata_get_all=async_mock.CoroutineMock(return_value={}),
                metadata_get=async_mock.CoroutineMock(return_value=True),
                save=async_mock.CoroutineMock(),
            )

            mock_conn_rec_cls.ACCEPT_AUTO = ConnRecord.ACCEPT_AUTO
            mock_conn_rec_cls.State.REQUEST = STATE_REQUEST
            mock_conn_rec_cls.State.get = async_mock.MagicMock(
                return_value=STATE_REQUEST
            )
            mock_conn_rec_cls.retrieve_by_id = async_mock.CoroutineMock(
                return_value=async_mock.MagicMock(save=async_mock.CoroutineMock())
            )
            mock_conn_rec_cls.retrieve_by_invitation_key = async_mock.CoroutineMock(
                return_value=mock_conn_record
            )
            mock_conn_rec_cls.return_value = mock_conn_record

            mock_did_posture.get = async_mock.MagicMock(
                return_value=test_module.DIDPosture.PUBLIC
            )

            mock_did_doc.from_json = async_mock.MagicMock(
                return_value=async_mock.MagicMock(did=TestConfig.test_did)
            )
            mock_attach_deco.data_base64 = async_mock.MagicMock(
                return_value=async_mock.MagicMock(
                    data=async_mock.MagicMock(sign=async_mock.CoroutineMock())
                )
            )
            mock_response.return_value = async_mock.MagicMock(
                assign_thread_from=async_mock.MagicMock(),
                assign_trace_from=async_mock.MagicMock(),
            )

            conn_rec = await self.manager.receive_request(
                request=mock_request,
                recipient_did=TestConfig.test_did,
                recipient_verkey=None,
                my_endpoint=None,
                alias=None,
                auto_accept_implicit=None,
                mediation_id=None,
            )
            assert conn_rec

        messages = self.responder.messages
        assert len(messages) == 2
        (result, target) = messages[0]
        assert "connection_id" in target

    async def test_receive_request_invi_not_found(self):
        mock_request = async_mock.MagicMock(
            did=TestConfig.test_did,
            did_doc_attach=None,
            _thread=async_mock.MagicMock(pthid="explicit-not-a-did"),
        )

        await self.session.wallet.create_local_did(
            method=DIDMethod.SOV,
            key_type=KeyType.ED25519,
            seed=None,
            did=TestConfig.test_did,
        )

        with async_mock.patch.object(
            test_module, "ConnRecord", async_mock.MagicMock()
        ) as mock_conn_rec_cls:
            mock_conn_rec_cls.retrieve_by_invitation_key = async_mock.CoroutineMock(
                side_effect=StorageNotFoundError()
            )
            with self.assertRaises(DIDXManagerError) as context:
                await self.manager.receive_request(
                    request=mock_request,
                    recipient_did=TestConfig.test_did,
                    recipient_verkey=TestConfig.test_verkey,
                    my_endpoint=None,
                    alias=None,
                    auto_accept_implicit=None,
                    mediation_id=None,
                )
            assert "No explicit invitation found" in str(context.exception)

    async def test_receive_request_with_mediator_without_multi_use_multitenant(self):
        multiuse_info = await self.session.wallet.create_local_did(
            method=DIDMethod.SOV,
            key_type=KeyType.ED25519,
        )
        did_doc_dict = self.make_did_doc(
            did=TestConfig.test_target_did,
            verkey=TestConfig.test_target_verkey,
        ).serialize()
        del did_doc_dict["authentication"]
        del did_doc_dict["service"]
        new_info = await self.session.wallet.create_local_did(
            method=DIDMethod.SOV,
            key_type=KeyType.ED25519,
        )

        mock_request = async_mock.MagicMock()
        mock_request.connection = async_mock.MagicMock(
            is_multiuse_invitation=False, invitation_key=multiuse_info.verkey
        )
        mock_request.connection.did = TestConfig.test_did
        mock_request.connection.did_doc = async_mock.MagicMock()
        mock_request.connection.did_doc.did = TestConfig.test_did
        mock_request.did = self.test_target_did
        mock_request.did_doc_attach = async_mock.MagicMock(
            data=async_mock.MagicMock(
                verify=async_mock.CoroutineMock(return_value=True),
                signed=async_mock.MagicMock(
                    decode=async_mock.MagicMock(return_value=json.dumps(did_doc_dict))
                ),
            )
        )

        await self.session.wallet.create_local_did(
            method=DIDMethod.SOV,
            key_type=KeyType.ED25519,
            seed=None,
            did=TestConfig.test_did,
        )

        mediation_record = MediationRecord(
            role=MediationRecord.ROLE_CLIENT,
            state=MediationRecord.STATE_GRANTED,
            connection_id=self.test_mediator_conn_id,
            routing_keys=self.test_mediator_routing_keys,
            endpoint=self.test_mediator_endpoint,
        )
        await mediation_record.save(self.session)

        record = ConnRecord(
            invitation_key=TestConfig.test_verkey,
            their_label="Hello",
            their_role=ConnRecord.Role.RESPONDER.rfc160,
            alias="Bob",
        )
        record.accept = ConnRecord.ACCEPT_MANUAL
        await record.save(self.session)

        with async_mock.patch.object(
            ConnRecord, "save", autospec=True
        ) as mock_conn_rec_save, async_mock.patch.object(
            ConnRecord, "attach_request", autospec=True
        ) as mock_conn_attach_request, async_mock.patch.object(
            ConnRecord, "retrieve_by_invitation_key"
        ) as mock_conn_retrieve_by_invitation_key, async_mock.patch.object(
            ConnRecord, "retrieve_request", autospec=True
        ):
            mock_conn_retrieve_by_invitation_key.return_value = record
            conn_rec = await self.manager.receive_request(
                request=mock_request,
                recipient_did=TestConfig.test_did,
                recipient_verkey=TestConfig.test_verkey,
                my_endpoint=None,
                alias=None,
                auto_accept_implicit=None,
                mediation_id=mediation_record.mediation_id,
            )

        assert len(self.responder.messages) == 1
        message, target = self.responder.messages[0]
        assert isinstance(message, KeylistUpdate)
        assert len(message.updates) == 1
        (remove,) = message.updates
        assert remove.action == KeylistUpdateRule.RULE_REMOVE
        assert remove.recipient_key == record.invitation_key

    async def test_receive_request_with_mediator_without_multi_use_multitenant_mismatch(
        self,
    ):
        multiuse_info = await self.session.wallet.create_local_did(
            method=DIDMethod.SOV,
            key_type=KeyType.ED25519,
        )
        did_doc_dict = self.make_did_doc(
            did=TestConfig.test_target_did,
            verkey=TestConfig.test_target_verkey,
        ).serialize()
        del did_doc_dict["authentication"]
        del did_doc_dict["service"]
        new_info = await self.session.wallet.create_local_did(
            method=DIDMethod.SOV,
            key_type=KeyType.ED25519,
        )

        mock_request = async_mock.MagicMock()
        mock_request.connection = async_mock.MagicMock(
            is_multiuse_invitation=False, invitation_key=multiuse_info.verkey
        )
        mock_request.connection.did = TestConfig.test_did
        mock_request.connection.did_doc = async_mock.MagicMock()
        mock_request.connection.did_doc.did = TestConfig.test_did
        mock_request.did_doc_attach = async_mock.MagicMock(
            data=async_mock.MagicMock(
                verify=async_mock.CoroutineMock(return_value=True),
                signed=async_mock.MagicMock(
                    decode=async_mock.MagicMock(return_value=json.dumps(did_doc_dict))
                ),
            )
        )

        await self.session.wallet.create_local_did(
            method=DIDMethod.SOV,
            key_type=KeyType.ED25519,
            seed=None,
            did=TestConfig.test_did,
        )

        mediation_record = MediationRecord(
            role=MediationRecord.ROLE_CLIENT,
            state=MediationRecord.STATE_GRANTED,
            connection_id=self.test_mediator_conn_id,
            routing_keys=self.test_mediator_routing_keys,
            endpoint=self.test_mediator_endpoint,
        )
        await mediation_record.save(self.session)

        record = ConnRecord(
            invitation_key=TestConfig.test_verkey,
            their_label="Hello",
            their_role=ConnRecord.Role.RESPONDER.rfc160,
            alias="Bob",
        )
        record.accept = ConnRecord.ACCEPT_MANUAL
        await record.save(self.session)

        with async_mock.patch.object(
            ConnRecord, "save", autospec=True
        ) as mock_conn_rec_save, async_mock.patch.object(
            ConnRecord, "attach_request", autospec=True
        ) as mock_conn_attach_request, async_mock.patch.object(
            ConnRecord, "retrieve_by_invitation_key"
        ) as mock_conn_retrieve_by_invitation_key, async_mock.patch.object(
            ConnRecord, "retrieve_request", autospec=True
        ):
            mock_conn_retrieve_by_invitation_key.return_value = record
            with self.assertRaises(DIDXManagerError) as context:
                conn_rec = await self.manager.receive_request(
                    request=mock_request,
                    recipient_did=TestConfig.test_did,
                    recipient_verkey=TestConfig.test_verkey,
                    my_endpoint=None,
                    alias=None,
                    auto_accept_implicit=None,
                    mediation_id=mediation_record.mediation_id,
                )
                assert "does not match" in str(context.exception)

    async def test_receive_request_public_did_no_did_doc_attachment(self):
        mock_request = async_mock.MagicMock(
            did=TestConfig.test_did,
            did_doc_attach=None,
            _thread=async_mock.MagicMock(pthid="did:sov:publicdid0000000000000"),
        )

        await self.session.wallet.create_local_did(
            method=DIDMethod.SOV,
            key_type=KeyType.ED25519,
            seed=None,
            did=TestConfig.test_did,
        )

        self.session.context.update_settings({"public_invites": True})
        mock_conn_rec_state_request = ConnRecord.State.REQUEST
        with async_mock.patch.object(
            test_module, "ConnRecord", async_mock.MagicMock()
        ) as mock_conn_rec_cls, async_mock.patch.object(
            test_module, "DIDPosture", autospec=True
        ) as mock_did_posture:
            mock_conn_record = async_mock.MagicMock(
                accept=ConnRecord.ACCEPT_MANUAL,
                my_did=None,
                state=mock_conn_rec_state_request.rfc23,
                attach_request=async_mock.CoroutineMock(),
                retrieve_request=async_mock.CoroutineMock(),
                metadata_get_all=async_mock.CoroutineMock(return_value={}),
                save=async_mock.CoroutineMock(),
            )
            mock_conn_rec_cls.return_value = mock_conn_record
            mock_conn_rec_cls.retrieve_by_invitation_key = async_mock.CoroutineMock(
                return_value=mock_conn_record
            )

            mock_did_posture.get = async_mock.MagicMock(
                return_value=test_module.DIDPosture.PUBLIC
            )

            with self.assertRaises(DIDXManagerError) as context:
                await self.manager.receive_request(
                    request=mock_request,
                    recipient_did=TestConfig.test_did,
                    recipient_verkey=None,
                    my_endpoint=TestConfig.test_endpoint,
                    alias="Alias",
                    auto_accept_implicit=None,
                    mediation_id=None,
                )
            assert "DID Doc attachment missing or has no data" in str(context.exception)

    async def test_receive_request_public_did_x_not_public(self):
        mock_request = async_mock.MagicMock(
            did=TestConfig.test_did,
            did_doc_attach=async_mock.MagicMock(
                data=async_mock.MagicMock(
                    verify=async_mock.CoroutineMock(return_value=True),
                    signed=async_mock.MagicMock(
                        decode=async_mock.MagicMock(return_value="dummy-did-doc")
                    ),
                )
            ),
            _thread=async_mock.MagicMock(pthid="did:sov:publicdid0000000000000"),
        )

        await self.session.wallet.create_local_did(
            method=DIDMethod.SOV,
            key_type=KeyType.ED25519,
            seed=None,
            did=TestConfig.test_did,
        )

        self.session.context.update_settings({"public_invites": True})
        mock_conn_rec_state_request = ConnRecord.State.REQUEST
        with async_mock.patch.object(
            test_module, "DIDPosture", autospec=True
        ) as mock_did_posture:
            mock_did_posture.get = async_mock.MagicMock(
                return_value=test_module.DIDPosture.WALLET_ONLY
            )

            with self.assertRaises(DIDXManagerError) as context:
                await self.manager.receive_request(
                    request=mock_request,
                    recipient_did=TestConfig.test_did,
                    recipient_verkey=None,
                    my_endpoint=TestConfig.test_endpoint,
                    alias="Alias",
                    auto_accept_implicit=False,
                    mediation_id=None,
                )

            assert "is not public" in str(context.exception)

    async def test_receive_request_public_did_x_wrong_did(self):
        mock_request = async_mock.MagicMock(
            did=TestConfig.test_did,
            did_doc_attach=async_mock.MagicMock(
                data=async_mock.MagicMock(
                    verify=async_mock.CoroutineMock(return_value=True),
                    signed=async_mock.MagicMock(
                        decode=async_mock.MagicMock(return_value="dummy-did-doc")
                    ),
                )
            ),
            _thread=async_mock.MagicMock(pthid="did:sov:publicdid0000000000000"),
        )

        await self.session.wallet.create_local_did(
            method=DIDMethod.SOV,
            key_type=KeyType.ED25519,
            seed=None,
            did=TestConfig.test_did,
        )

        self.session.context.update_settings({"public_invites": True})
        mock_conn_rec_state_request = ConnRecord.State.REQUEST
        with async_mock.patch.object(
            test_module, "ConnRecord", async_mock.MagicMock()
        ) as mock_conn_rec_cls, async_mock.patch.object(
            test_module, "DIDPosture", autospec=True
        ) as mock_did_posture, async_mock.patch.object(
            test_module.DIDDoc, "from_json", async_mock.MagicMock()
        ) as mock_did_doc_from_json:
            mock_conn_record = async_mock.MagicMock(
                accept=ConnRecord.ACCEPT_MANUAL,
                my_did=None,
                state=mock_conn_rec_state_request.rfc23,
                attach_request=async_mock.CoroutineMock(),
                retrieve_request=async_mock.CoroutineMock(),
                metadata_get_all=async_mock.CoroutineMock(return_value={}),
                save=async_mock.CoroutineMock(),
            )
            mock_conn_rec_cls.return_value = mock_conn_record
            mock_conn_rec_cls.retrieve_by_invitation_key = async_mock.CoroutineMock(
                return_value=mock_conn_record
            )
            mock_did_doc_from_json.return_value = async_mock.MagicMock(did="wrong-did")

            mock_did_posture.get = async_mock.MagicMock(
                return_value=test_module.DIDPosture.PUBLIC
            )

            with self.assertRaises(DIDXManagerError) as context:
                await self.manager.receive_request(
                    request=mock_request,
                    recipient_did=TestConfig.test_did,
                    recipient_verkey=None,
                    my_endpoint=TestConfig.test_endpoint,
                    alias="Alias",
                    auto_accept_implicit=False,
                    mediation_id=None,
                )

            assert "does not match" in str(context.exception)

    async def test_receive_request_public_did_x_did_doc_attach_bad_sig(self):
        mock_request = async_mock.MagicMock(
            did=TestConfig.test_did,
            did_doc_attach=async_mock.MagicMock(
                data=async_mock.MagicMock(
                    verify=async_mock.CoroutineMock(return_value=False)
                )
            ),
            _thread=async_mock.MagicMock(pthid="did:sov:publicdid0000000000000"),
        )

        await self.session.wallet.create_local_did(
            method=DIDMethod.SOV,
            key_type=KeyType.ED25519,
            seed=None,
            did=TestConfig.test_did,
        )

        self.session.context.update_settings({"public_invites": True})
        mock_conn_rec_state_request = ConnRecord.State.REQUEST
        with async_mock.patch.object(
            test_module, "ConnRecord", async_mock.MagicMock()
        ) as mock_conn_rec_cls, async_mock.patch.object(
            test_module, "DIDPosture", autospec=True
        ) as mock_did_posture:
            mock_conn_record = async_mock.MagicMock(
                accept=ConnRecord.ACCEPT_MANUAL,
                my_did=None,
                state=mock_conn_rec_state_request.rfc23,
                attach_request=async_mock.CoroutineMock(),
                retrieve_request=async_mock.CoroutineMock(),
                metadata_get_all=async_mock.CoroutineMock(return_value={}),
                save=async_mock.CoroutineMock(),
            )
            mock_conn_rec_cls.return_value = mock_conn_record
            mock_conn_rec_cls.retrieve_by_invitation_key = async_mock.CoroutineMock(
                return_value=mock_conn_record
            )

            mock_did_posture.get = async_mock.MagicMock(
                return_value=test_module.DIDPosture.PUBLIC
            )

            with self.assertRaises(DIDXManagerError) as context:
                await self.manager.receive_request(
                    request=mock_request,
                    recipient_did=TestConfig.test_did,
                    recipient_verkey=None,
                    my_endpoint=TestConfig.test_endpoint,
                    alias="Alias",
                    auto_accept_implicit=False,
                    mediation_id=None,
                )
            assert "DID Doc signature failed" in str(context.exception)

    async def test_receive_request_public_did_no_public_invites(self):
        mock_request = async_mock.MagicMock(
            did=TestConfig.test_did,
            did_doc_attach=async_mock.MagicMock(
                data=async_mock.MagicMock(
                    verify=async_mock.CoroutineMock(return_value=True),
                    signed=async_mock.MagicMock(
                        decode=async_mock.MagicMock(return_value="dummy-did-doc")
                    ),
                )
            ),
            _thread=async_mock.MagicMock(pthid="did:sov:publicdid0000000000000"),
        )

        await self.session.wallet.create_local_did(
            method=DIDMethod.SOV,
            key_type=KeyType.ED25519,
            seed=None,
            did=TestConfig.test_did,
        )

        self.session.context.update_settings({"public_invites": False})
        with async_mock.patch.object(
            test_module, "ConnRecord", async_mock.MagicMock()
        ) as mock_conn_rec_cls, async_mock.patch.object(
            test_module, "AttachDecorator", autospec=True
        ) as mock_attach_deco, async_mock.patch.object(
            test_module, "DIDXResponse", autospec=True
        ) as mock_response, async_mock.patch.object(
            self.manager, "create_did_document", async_mock.CoroutineMock()
        ) as mock_create_did_doc, async_mock.patch.object(
            test_module.DIDDoc, "from_json", async_mock.MagicMock()
        ) as mock_did_doc_from_json:
            mock_did_doc_from_json.return_value = async_mock.MagicMock(
                did=TestConfig.test_did
            )
            with self.assertRaises(DIDXManagerError) as context:
                await self.manager.receive_request(
                    request=mock_request,
                    recipient_did=TestConfig.test_did,
                    recipient_verkey=None,
                    my_endpoint=TestConfig.test_endpoint,
                    alias="Alias",
                    auto_accept_implicit=False,
                    mediation_id=None,
                )
            assert "Public invitations are not enabled" in str(context.exception)

    async def test_receive_request_public_did_no_auto_accept(self):
        mock_request = async_mock.MagicMock(
            did=TestConfig.test_did,
            did_doc_attach=async_mock.MagicMock(
                data=async_mock.MagicMock(
                    verify=async_mock.CoroutineMock(return_value=True),
                    signed=async_mock.MagicMock(
                        decode=async_mock.MagicMock(return_value="dummy-did-doc")
                    ),
                )
            ),
            _thread=async_mock.MagicMock(pthid="did:sov:publicdid0000000000000"),
        )

        await self.session.wallet.create_local_did(
            method=DIDMethod.SOV,
            key_type=KeyType.ED25519,
            seed=None,
            did=TestConfig.test_did,
        )

        self.session.context.update_settings(
            {"public_invites": True, "debug.auto_accept_requests": False}
        )
        mock_conn_rec_state_request = ConnRecord.State.REQUEST
        with async_mock.patch.object(
            test_module, "ConnRecord", async_mock.MagicMock()
        ) as mock_conn_rec_cls, async_mock.patch.object(
            test_module, "DIDDoc", autospec=True
        ) as mock_did_doc, async_mock.patch.object(
            test_module, "DIDPosture", autospec=True
        ) as mock_did_posture, async_mock.patch.object(
            test_module, "AttachDecorator", autospec=True
        ) as mock_attach_deco, async_mock.patch.object(
            test_module, "DIDXResponse", autospec=True
        ) as mock_response, async_mock.patch.object(
            self.manager, "create_did_document", async_mock.CoroutineMock()
        ) as mock_create_did_doc:
            mock_conn_record = async_mock.MagicMock(
                accept=ConnRecord.ACCEPT_MANUAL,
                my_did=None,
                state=mock_conn_rec_state_request.rfc23,
                attach_request=async_mock.CoroutineMock(),
                retrieve_request=async_mock.CoroutineMock(),
                metadata_get_all=async_mock.CoroutineMock(return_value={}),
                save=async_mock.CoroutineMock(),
            )
            mock_conn_rec_cls.return_value = mock_conn_record
            mock_conn_rec_cls.retrieve_by_invitation_key = async_mock.CoroutineMock(
                return_value=mock_conn_record
            )

            mock_did_posture.get = async_mock.MagicMock(
                return_value=test_module.DIDPosture.PUBLIC
            )

            mock_did_doc.from_json = async_mock.MagicMock(
                return_value=async_mock.MagicMock(did=TestConfig.test_did)
            )
            conn_rec = await self.manager.receive_request(
                request=mock_request,
                recipient_did=TestConfig.test_did,
                recipient_verkey=None,
                my_endpoint=TestConfig.test_endpoint,
                alias="Alias",
                auto_accept_implicit=False,
                mediation_id=None,
            )
            assert conn_rec

        messages = self.responder.messages
        assert not messages

    async def test_receive_request_peer_did(self):
        mock_request = async_mock.MagicMock(
            did=TestConfig.test_did,
            did_doc_attach=async_mock.MagicMock(
                data=async_mock.MagicMock(
                    verify=async_mock.CoroutineMock(return_value=True),
                    signed=async_mock.MagicMock(
                        decode=async_mock.MagicMock(return_value="dummy-did-doc")
                    ),
                )
            ),
            _thread=async_mock.MagicMock(pthid="dummy-pthid"),
        )

        mock_conn = async_mock.MagicMock(
            my_did=TestConfig.test_did,
            their_did=TestConfig.test_target_did,
            invitation_key=TestConfig.test_verkey,
            connection_id="dummy",
            is_multiuse_invitation=True,
            state=ConnRecord.State.INVITATION.rfc23,
            their_role=ConnRecord.Role.REQUESTER.rfc23,
            save=async_mock.CoroutineMock(),
            attach_request=async_mock.CoroutineMock(),
            accept=ConnRecord.ACCEPT_MANUAL,
            metadata_get_all=async_mock.CoroutineMock(return_value={"test": "value"}),
        )
        mock_conn_rec_state_request = ConnRecord.State.REQUEST

        await self.session.wallet.create_local_did(
            method=DIDMethod.SOV,
            key_type=KeyType.ED25519,
            seed=None,
            did=TestConfig.test_did,
        )

        self.session.context.update_settings({"public_invites": True})
        with async_mock.patch.object(
            test_module, "ConnRecord", async_mock.MagicMock()
        ) as mock_conn_rec_cls, async_mock.patch.object(
            test_module, "DIDDoc", autospec=True
        ) as mock_did_doc, async_mock.patch.object(
            test_module, "AttachDecorator", autospec=True
        ) as mock_attach_deco, async_mock.patch.object(
            test_module, "DIDXResponse", autospec=True
        ) as mock_response:
            mock_conn_rec_cls.retrieve_by_invitation_key = async_mock.CoroutineMock(
                return_value=mock_conn
            )
            mock_conn_rec_cls.return_value = async_mock.MagicMock(
                accept=ConnRecord.ACCEPT_AUTO,
                my_did=None,
                state=mock_conn_rec_state_request.rfc23,
                attach_request=async_mock.CoroutineMock(),
                retrieve_request=async_mock.CoroutineMock(),
                save=async_mock.CoroutineMock(),
                metadata_set=async_mock.CoroutineMock(),
            )
            mock_did_doc.from_json = async_mock.MagicMock(
                return_value=async_mock.MagicMock(did=TestConfig.test_did)
            )
            mock_attach_deco.data_base64 = async_mock.MagicMock(
                return_value=async_mock.MagicMock(
                    data=async_mock.MagicMock(sign=async_mock.CoroutineMock())
                )
            )
            mock_response.return_value = async_mock.MagicMock(
                assign_thread_from=async_mock.MagicMock(),
                assign_trace_from=async_mock.MagicMock(),
            )

            conn_rec = await self.manager.receive_request(
                request=mock_request,
                recipient_did=TestConfig.test_did,
                recipient_verkey=TestConfig.test_verkey,
                my_endpoint=TestConfig.test_endpoint,
                alias="Alias",
                auto_accept_implicit=False,
                mediation_id=None,
            )
            assert conn_rec
            mock_conn_rec_cls.return_value.metadata_set.assert_called()

        assert not self.responder.messages

    async def test_receive_request_multiuse_multitenant(self):
        multiuse_info = await self.session.wallet.create_local_did(
            method=DIDMethod.SOV,
            key_type=KeyType.ED25519,
        )
        new_info = await self.session.wallet.create_local_did(
            method=DIDMethod.SOV,
            key_type=KeyType.ED25519,
        )

        mock_request = async_mock.MagicMock(
            did=TestConfig.test_did,
            did_doc_attach=async_mock.MagicMock(
                data=async_mock.MagicMock(
                    verify=async_mock.CoroutineMock(return_value=True),
                    signed=async_mock.MagicMock(
                        decode=async_mock.MagicMock(return_value="dummy-did-doc")
                    ),
                )
            ),
            _thread=async_mock.MagicMock(pthid="dummy-pthid"),
        )

        self.context.update_settings(
            {"wallet.id": "test_wallet", "multitenant.enabled": True}
        )
        ACCEPT_MANUAL = ConnRecord.ACCEPT_MANUAL
        with async_mock.patch.object(
            test_module, "ConnRecord", autospec=True
        ) as mock_conn_rec_cls, async_mock.patch.object(
            InMemoryWallet, "create_local_did", autospec=True
        ) as mock_wallet_create_local_did, async_mock.patch.object(
            test_module, "DIDDoc", autospec=True
        ) as mock_did_doc:
            mock_conn_rec = async_mock.CoroutineMock(
                connection_id="dummy",
                accept=ACCEPT_MANUAL,
                is_multiuse_invitation=True,
                attach_request=async_mock.CoroutineMock(),
                save=async_mock.CoroutineMock(),
                retrieve_invitation=async_mock.CoroutineMock(return_value={}),
                metadata_get_all=async_mock.CoroutineMock(return_value={}),
                retrieve_request=async_mock.CoroutineMock(),
            )
            mock_conn_rec_cls.return_value = mock_conn_rec
            mock_conn_rec_cls.retrieve_by_invitation_key = async_mock.CoroutineMock(
                return_value=mock_conn_rec
            )
            mock_wallet_create_local_did.return_value = DIDInfo(
                new_info.did,
                new_info.verkey,
                None,
                method=DIDMethod.SOV,
                key_type=KeyType.ED25519,
            )
            mock_did_doc.from_json = async_mock.MagicMock(
                return_value=async_mock.MagicMock(did=TestConfig.test_did)
            )
            await self.manager.receive_request(
                request=mock_request,
                recipient_did=TestConfig.test_did,
                recipient_verkey=TestConfig.test_verkey,
                my_endpoint=TestConfig.test_endpoint,
                alias="Alias",
                auto_accept_implicit=False,
                mediation_id=None,
            )

            self.multitenant_mgr.add_key.assert_called_once_with(
                "test_wallet", new_info.verkey
            )

    async def test_receive_request_implicit_multitenant(self):
        new_info = await self.session.wallet.create_local_did(
            method=DIDMethod.SOV,
            key_type=KeyType.ED25519,
        )

        mock_request = async_mock.MagicMock(
            did=TestConfig.test_did,
            did_doc_attach=async_mock.MagicMock(
                data=async_mock.MagicMock(
                    verify=async_mock.CoroutineMock(return_value=True),
                    signed=async_mock.MagicMock(
                        decode=async_mock.MagicMock(return_value="dummy-did-doc")
                    ),
                )
            ),
            _thread=async_mock.MagicMock(pthid="did:sov:publicdid0000000000000"),
        )

        self.context.update_settings(
            {
                "wallet.id": "test_wallet",
                "multitenant.enabled": True,
                "public_invites": True,
                "debug.auto_accept_requests": False,
            }
        )

        ACCEPT_MANUAL = ConnRecord.ACCEPT_MANUAL
        with async_mock.patch.object(
            test_module, "ConnRecord", autospec=True
        ) as mock_conn_rec_cls, async_mock.patch.object(
            InMemoryWallet, "create_local_did", autospec=True
        ) as mock_wallet_create_local_did, async_mock.patch.object(
            InMemoryWallet, "get_local_did", autospec=True
        ) as mock_wallet_get_local_did, async_mock.patch.object(
            test_module, "DIDPosture", autospec=True
        ) as mock_did_posture, async_mock.patch.object(
            test_module, "DIDDoc", autospec=True
        ) as mock_did_doc:
            mock_conn_rec = async_mock.CoroutineMock(
                connection_id="dummy",
                accept=ACCEPT_MANUAL,
                is_multiuse_invitation=False,
                attach_request=async_mock.CoroutineMock(),
                save=async_mock.CoroutineMock(),
                retrieve_invitation=async_mock.CoroutineMock(return_value={}),
                metadata_get_all=async_mock.CoroutineMock(return_value={}),
                retrieve_request=async_mock.CoroutineMock(),
            )
            mock_conn_rec_cls.return_value = mock_conn_rec
            mock_conn_rec_cls.retrieve_by_invitation_key = async_mock.CoroutineMock(
                side_effect=StorageNotFoundError()
            )

            mock_did_posture.get = async_mock.MagicMock(
                return_value=test_module.DIDPosture.PUBLIC
            )

            mock_wallet_create_local_did.return_value = DIDInfo(
                new_info.did,
                new_info.verkey,
                None,
                method=DIDMethod.SOV,
                key_type=KeyType.ED25519,
            )
            mock_did_doc.from_json = async_mock.MagicMock(
                return_value=async_mock.MagicMock(did=TestConfig.test_did)
            )
            mock_wallet_get_local_did.return_value = DIDInfo(
                TestConfig.test_did,
                TestConfig.test_verkey,
                None,
                method=DIDMethod.SOV,
                key_type=KeyType.ED25519,
            )
            await self.manager.receive_request(
                request=mock_request,
                recipient_did=TestConfig.test_did,
                recipient_verkey=None,
                my_endpoint=TestConfig.test_endpoint,
                alias="Alias",
                auto_accept_implicit=False,
                mediation_id=None,
            )

            self.multitenant_mgr.add_key.assert_called_once_with(
                "test_wallet", new_info.verkey
            )

    async def test_receive_request_peer_did_not_found_x(self):
        mock_request = async_mock.MagicMock(
            did=TestConfig.test_did,
            did_doc_attach=async_mock.MagicMock(
                data=async_mock.MagicMock(
                    verify=async_mock.CoroutineMock(return_value=True),
                    signed=async_mock.MagicMock(
                        decode=async_mock.MagicMock(return_value="dummy-did-doc")
                    ),
                )
            ),
            _thread=async_mock.MagicMock(pthid="dummy-pthid"),
        )

        await self.session.wallet.create_local_did(
            method=DIDMethod.SOV,
            key_type=KeyType.ED25519,
            seed=None,
            did=TestConfig.test_did,
        )

        with async_mock.patch.object(
            test_module, "ConnRecord", async_mock.MagicMock()
        ) as mock_conn_rec_cls:
            mock_conn_rec_cls.retrieve_by_invitation_key = async_mock.CoroutineMock(
                side_effect=StorageNotFoundError()
            )
            with self.assertRaises(DIDXManagerError):
                await self.manager.receive_request(
                    request=mock_request,
                    recipient_did=TestConfig.test_did,
                    recipient_verkey=TestConfig.test_verkey,
                    my_endpoint=TestConfig.test_endpoint,
                    alias="Alias",
                    auto_accept_implicit=False,
                    mediation_id=None,
                )

    async def test_create_response(self):
        conn_rec = ConnRecord(
            connection_id="dummy", state=ConnRecord.State.REQUEST.rfc23
        )

        with async_mock.patch.object(
            test_module.ConnRecord, "retrieve_request", async_mock.CoroutineMock()
        ) as mock_retrieve_req, async_mock.patch.object(
            conn_rec, "save", async_mock.CoroutineMock()
        ) as mock_save, async_mock.patch.object(
            test_module, "DIDDoc", autospec=True
        ) as mock_did_doc, async_mock.patch.object(
            test_module, "AttachDecorator", autospec=True
        ) as mock_attach_deco, async_mock.patch.object(
            test_module, "DIDXResponse", autospec=True
        ) as mock_response, async_mock.patch.object(
            self.manager, "create_did_document", async_mock.CoroutineMock()
        ) as mock_create_did_doc:
            mock_create_did_doc.return_value = async_mock.MagicMock(
                serialize=async_mock.MagicMock()
            )
            mock_attach_deco.data_base64 = async_mock.MagicMock(
                return_value=async_mock.MagicMock(
                    data=async_mock.MagicMock(sign=async_mock.CoroutineMock())
                )
            )

            await self.manager.create_response(conn_rec, "http://10.20.30.40:5060/")

    async def test_create_response_mediation_id(self):
        mediation_record = MediationRecord(
            role=MediationRecord.ROLE_CLIENT,
            state=MediationRecord.STATE_GRANTED,
            connection_id=self.test_mediator_conn_id,
            routing_keys=self.test_mediator_routing_keys,
            endpoint=self.test_mediator_endpoint,
        )
        await mediation_record.save(self.session)

        invi = InvitationMessage(
            comment="test",
            handshake_protocols=[
                pfx.qualify(HSProto.RFC23.name) for pfx in DIDCommPrefix
            ],
            services=[TestConfig.test_did],
        )
        record = ConnRecord(
            invitation_key=TestConfig.test_verkey,
            invitation_msg_id=invi._id,
            their_label="Hello",
            their_role=ConnRecord.Role.RESPONDER.rfc160,
            alias="Bob",
            my_did=None,
            state=ConnRecord.State.REQUEST.rfc23,
        )

        await record.save(self.session)
        await record.attach_invitation(self.session, invi)

        with async_mock.patch.object(
            ConnRecord, "log_state", autospec=True
        ) as mock_conn_log_state, async_mock.patch.object(
            ConnRecord, "retrieve_request", autospec=True
        ) as mock_conn_retrieve_request, async_mock.patch.object(
            ConnRecord, "save", autospec=True
        ) as mock_conn_save, async_mock.patch.object(
            record, "metadata_get", async_mock.CoroutineMock(return_value=False)
        ), async_mock.patch.object(
            test_module, "AttachDecorator", autospec=True
        ) as mock_attach_deco:
            mock_attach_deco.data_base64 = async_mock.MagicMock(
                return_value=async_mock.MagicMock(
                    data=async_mock.MagicMock(sign=async_mock.CoroutineMock())
                )
            )
            await self.manager.create_response(
                record, mediation_id=mediation_record.mediation_id
            )

        assert len(self.responder.messages) == 1
        message, target = self.responder.messages[0]
        assert isinstance(message, KeylistUpdate)
        assert len(message.updates) == 1
        (add,) = message.updates
        assert add.action == KeylistUpdateRule.RULE_ADD
        assert add.recipient_key

    async def test_create_response_mediation_id_invalid_conn_state(self):
        mediation_record = MediationRecord(
            role=MediationRecord.ROLE_CLIENT,
            state=MediationRecord.STATE_GRANTED,
            connection_id=self.test_mediator_conn_id,
            routing_keys=self.test_mediator_routing_keys,
            endpoint=self.test_mediator_endpoint,
        )
        await mediation_record.save(self.session)

        invi = InvitationMessage(
            comment="test",
            handshake_protocols=[
                pfx.qualify(HSProto.RFC23.name) for pfx in DIDCommPrefix
            ],
            services=[TestConfig.test_did],
        )
        record = ConnRecord(
            invitation_key=TestConfig.test_verkey,
            invitation_msg_id=invi._id,
            their_label="Hello",
            their_role=ConnRecord.Role.RESPONDER.rfc160,
            alias="Bob",
            my_did=None,
        )

        await record.save(self.session)
        await record.attach_invitation(self.session, invi)

        with async_mock.patch.object(
            ConnRecord, "log_state", autospec=True
        ) as mock_conn_log_state, async_mock.patch.object(
            ConnRecord, "retrieve_request", autospec=True
        ) as mock_conn_retrieve_request, async_mock.patch.object(
            ConnRecord, "save", autospec=True
        ) as mock_conn_save, async_mock.patch.object(
            record, "metadata_get", async_mock.CoroutineMock(return_value=False)
        ):
            with self.assertRaises(DIDXManagerError) as context:
                await self.manager.create_response(
                    record, mediation_id=mediation_record.mediation_id
                )
                assert "Connection not in state" in str(context.exception)

    async def test_create_response_multitenant(self):
        conn_rec = ConnRecord(
            connection_id="dummy", state=ConnRecord.State.REQUEST.rfc23
        )

        self.manager.session.context.update_settings(
            {
                "multitenant.enabled": True,
                "wallet.id": "test_wallet",
            }
        )

        with async_mock.patch.object(
            test_module.ConnRecord, "retrieve_request"
        ), async_mock.patch.object(
            conn_rec, "save", async_mock.CoroutineMock()
        ), async_mock.patch.object(
            test_module, "AttachDecorator", autospec=True
        ) as mock_attach_deco, async_mock.patch.object(
            self.manager, "create_did_document", async_mock.CoroutineMock()
        ) as mock_create_did_doc, async_mock.patch.object(
            InMemoryWallet, "create_local_did", autospec=True
        ) as mock_wallet_create_local_did:
            mock_wallet_create_local_did.return_value = DIDInfo(
                TestConfig.test_did,
                TestConfig.test_verkey,
                None,
                method=DIDMethod.SOV,
                key_type=KeyType.ED25519,
            )
            mock_create_did_doc.return_value = async_mock.MagicMock(
                serialize=async_mock.MagicMock()
            )
            mock_attach_deco.data_base64 = async_mock.MagicMock(
                return_value=async_mock.MagicMock(
                    data=async_mock.MagicMock(sign=async_mock.CoroutineMock())
                )
            )

            await self.manager.create_response(conn_rec)
            self.multitenant_mgr.add_key.assert_called_once_with(
                "test_wallet", TestConfig.test_verkey
            )

    async def test_create_response_conn_rec_my_did(self):
        conn_rec = ConnRecord(
            connection_id="dummy",
            my_did=TestConfig.test_did,
            state=ConnRecord.State.REQUEST.rfc23,
        )

        with async_mock.patch.object(
            test_module.ConnRecord, "retrieve_request", async_mock.CoroutineMock()
        ) as mock_retrieve_req, async_mock.patch.object(
            conn_rec, "save", async_mock.CoroutineMock()
        ) as mock_save, async_mock.patch.object(
            test_module, "DIDDoc", autospec=True
        ) as mock_did_doc, async_mock.patch.object(
            test_module, "AttachDecorator", autospec=True
        ) as mock_attach_deco, async_mock.patch.object(
            test_module, "DIDXResponse", autospec=True
        ) as mock_response, async_mock.patch.object(
            self.manager, "create_did_document", async_mock.CoroutineMock()
        ) as mock_create_did_doc, async_mock.patch.object(
            InMemoryWallet, "get_local_did", async_mock.CoroutineMock()
        ) as mock_get_loc_did:
            mock_get_loc_did.return_value = self.did_info
            mock_create_did_doc.return_value = async_mock.MagicMock(
                serialize=async_mock.MagicMock()
            )
            mock_attach_deco.data_base64 = async_mock.MagicMock(
                return_value=async_mock.MagicMock(
                    data=async_mock.MagicMock(sign=async_mock.CoroutineMock())
                )
            )

            await self.manager.create_response(conn_rec, "http://10.20.30.40:5060/")

    async def test_create_response_bad_state(self):
        with self.assertRaises(DIDXManagerError):
            await self.manager.create_response(
                ConnRecord(
                    invitation_key=TestConfig.test_verkey,
                    their_label="Hello",
                    their_role=ConnRecord.Role.REQUESTER.rfc23,
                    state=ConnRecord.State.ABANDONED.rfc23,
                    alias="Bob",
                )
            )

    async def test_accept_response_find_by_thread_id(self):
        mock_response = async_mock.MagicMock()
        mock_response._thread = async_mock.MagicMock()
        mock_response.did = TestConfig.test_target_did
        mock_response.did_doc_attach = async_mock.MagicMock(
            data=async_mock.MagicMock(
                verify=async_mock.CoroutineMock(return_value=True),
                signed=async_mock.MagicMock(
                    decode=async_mock.MagicMock(
                        return_value=json.dumps({"dummy": "did-doc"})
                    )
                ),
            )
        )

        receipt = MessageReceipt(
            recipient_did=TestConfig.test_did,
            recipient_did_public=True,
        )

        with async_mock.patch.object(
            ConnRecord, "save", autospec=True
        ) as mock_conn_rec_save, async_mock.patch.object(
            ConnRecord, "retrieve_by_request_id", async_mock.CoroutineMock()
        ) as mock_conn_retrieve_by_req_id, async_mock.patch.object(
            ConnRecord, "retrieve_by_id", async_mock.CoroutineMock()
        ) as mock_conn_retrieve_by_id, async_mock.patch.object(
            DIDDoc, "deserialize", async_mock.MagicMock()
        ) as mock_did_doc_deser:
            mock_did_doc_deser.return_value = async_mock.MagicMock(
                did=TestConfig.test_target_did
            )
            mock_conn_retrieve_by_req_id.return_value = async_mock.MagicMock(
                did=TestConfig.test_target_did,
                did_doc_attach=async_mock.MagicMock(
                    data=async_mock.MagicMock(
                        verify=async_mock.CoroutineMock(return_value=True),
                        signed=async_mock.MagicMock(
                            decode=async_mock.MagicMock(
                                return_value=json.dumps({"dummy": "did-doc"})
                            )
                        ),
                    )
                ),
                state=ConnRecord.State.REQUEST.rfc23,
                save=async_mock.CoroutineMock(),
                metadata_get=async_mock.CoroutineMock(),
                connection_id="test-conn-id",
            )
            mock_conn_retrieve_by_id.return_value = async_mock.MagicMock(
                their_did=TestConfig.test_target_did,
                save=async_mock.CoroutineMock(),
            )

            conn_rec = await self.manager.accept_response(mock_response, receipt)
            assert conn_rec.their_did == TestConfig.test_target_did
            assert ConnRecord.State.get(conn_rec.state) is ConnRecord.State.COMPLETED

    async def test_accept_response_not_found_by_thread_id_receipt_has_sender_did(self):
        mock_response = async_mock.MagicMock()
        mock_response._thread = async_mock.MagicMock()
        mock_response.did = TestConfig.test_target_did
        mock_response.did_doc_attach = async_mock.MagicMock(
            data=async_mock.MagicMock(
                verify=async_mock.CoroutineMock(return_value=True),
                signed=async_mock.MagicMock(
                    decode=async_mock.MagicMock(
                        return_value=json.dumps({"dummy": "did-doc"})
                    )
                ),
            )
        )

        receipt = MessageReceipt(sender_did=TestConfig.test_target_did)

        with async_mock.patch.object(
            ConnRecord, "save", autospec=True
        ) as mock_conn_rec_save, async_mock.patch.object(
            ConnRecord, "retrieve_by_request_id", async_mock.CoroutineMock()
        ) as mock_conn_retrieve_by_req_id, async_mock.patch.object(
            ConnRecord, "retrieve_by_did", async_mock.CoroutineMock()
        ) as mock_conn_retrieve_by_did, async_mock.patch.object(
            DIDDoc, "deserialize", async_mock.MagicMock()
        ) as mock_did_doc_deser:
            mock_did_doc_deser.return_value = async_mock.MagicMock(
                did=TestConfig.test_target_did
            )
            mock_conn_retrieve_by_req_id.side_effect = StorageNotFoundError()
            mock_conn_retrieve_by_did.return_value = async_mock.MagicMock(
                did=TestConfig.test_target_did,
                did_doc_attach=async_mock.MagicMock(
                    data=async_mock.MagicMock(
                        verify=async_mock.CoroutineMock(return_value=True),
                        signed=async_mock.MagicMock(
                            decode=async_mock.MagicMock(
                                return_value=json.dumps({"dummy": "did-doc"})
                            )
                        ),
                    )
                ),
                state=ConnRecord.State.REQUEST.rfc23,
                save=async_mock.CoroutineMock(),
                metadata_get=async_mock.CoroutineMock(return_value=False),
                connection_id="test-conn-id",
            )

            conn_rec = await self.manager.accept_response(mock_response, receipt)
            assert conn_rec.their_did == TestConfig.test_target_did
            assert ConnRecord.State.get(conn_rec.state) is ConnRecord.State.COMPLETED

    async def test_accept_response_not_found_by_thread_id_nor_receipt_sender_did(self):
        mock_response = async_mock.MagicMock()
        mock_response._thread = async_mock.MagicMock()
        mock_response.did = TestConfig.test_target_did
        mock_response.did_doc_attach = async_mock.MagicMock(
            data=async_mock.MagicMock(
                verify=async_mock.CoroutineMock(return_value=True),
                signed=async_mock.MagicMock(
                    decode=async_mock.MagicMock(
                        return_value=json.dumps({"dummy": "did-doc"})
                    )
                ),
            )
        )

        receipt = MessageReceipt(sender_did=TestConfig.test_target_did)

        with async_mock.patch.object(
            ConnRecord, "save", autospec=True
        ) as mock_conn_rec_save, async_mock.patch.object(
            ConnRecord, "retrieve_by_request_id", async_mock.CoroutineMock()
        ) as mock_conn_retrieve_by_req_id, async_mock.patch.object(
            ConnRecord, "retrieve_by_did", async_mock.CoroutineMock()
        ) as mock_conn_retrieve_by_did:
            mock_conn_retrieve_by_req_id.side_effect = StorageNotFoundError()
            mock_conn_retrieve_by_did.side_effect = StorageNotFoundError()

            with self.assertRaises(DIDXManagerError):
                await self.manager.accept_response(mock_response, receipt)

    async def test_accept_response_find_by_thread_id_bad_state(self):
        mock_response = async_mock.MagicMock()
        mock_response._thread = async_mock.MagicMock()
        mock_response.did = TestConfig.test_target_did
        mock_response.did_doc_attach = async_mock.MagicMock(
            data=async_mock.MagicMock(
                verify=async_mock.CoroutineMock(return_value=True),
                signed=async_mock.MagicMock(
                    decode=async_mock.MagicMock(
                        return_value=json.dumps({"dummy": "did-doc"})
                    )
                ),
            )
        )

        receipt = MessageReceipt(sender_did=TestConfig.test_target_did)

        with async_mock.patch.object(
            ConnRecord, "save", autospec=True
        ) as mock_conn_rec_save, async_mock.patch.object(
            ConnRecord, "retrieve_by_request_id", async_mock.CoroutineMock()
        ) as mock_conn_retrieve_by_req_id:
            mock_conn_retrieve_by_req_id.return_value = async_mock.MagicMock(
                state=ConnRecord.State.ABANDONED.rfc23
            )

            with self.assertRaises(DIDXManagerError):
                await self.manager.accept_response(mock_response, receipt)

    async def test_accept_response_find_by_thread_id_no_did_doc_attached(self):
        mock_response = async_mock.MagicMock()
        mock_response._thread = async_mock.MagicMock()
        mock_response.did = TestConfig.test_target_did
        mock_response.did_doc_attach = None

        receipt = MessageReceipt(sender_did=TestConfig.test_target_did)

        with async_mock.patch.object(
            ConnRecord, "save", autospec=True
        ) as mock_conn_rec_save, async_mock.patch.object(
            ConnRecord, "retrieve_by_request_id", async_mock.CoroutineMock()
        ) as mock_conn_retrieve_by_req_id:
            mock_conn_retrieve_by_req_id.return_value = async_mock.MagicMock(
                did=TestConfig.test_target_did,
                did_doc_attach=async_mock.MagicMock(
                    data=async_mock.MagicMock(
                        verify=async_mock.CoroutineMock(return_value=True),
                        signed=async_mock.MagicMock(
                            decode=async_mock.MagicMock(
                                return_value=json.dumps({"dummy": "did-doc"})
                            )
                        ),
                    )
                ),
                state=ConnRecord.State.REQUEST.rfc23,
                save=async_mock.CoroutineMock(),
            )

            with self.assertRaises(DIDXManagerError):
                await self.manager.accept_response(mock_response, receipt)

    async def test_accept_response_find_by_thread_id_did_mismatch(self):
        mock_response = async_mock.MagicMock()
        mock_response._thread = async_mock.MagicMock()
        mock_response.did = TestConfig.test_target_did
        mock_response.did_doc_attach = async_mock.MagicMock(
            data=async_mock.MagicMock(
                verify=async_mock.CoroutineMock(return_value=True),
                signed=async_mock.MagicMock(
                    decode=async_mock.MagicMock(
                        return_value=json.dumps({"dummy": "did-doc"})
                    )
                ),
            )
        )

        receipt = MessageReceipt(sender_did=TestConfig.test_target_did)

        with async_mock.patch.object(
            ConnRecord, "save", autospec=True
        ) as mock_conn_rec_save, async_mock.patch.object(
            ConnRecord, "retrieve_by_request_id", async_mock.CoroutineMock()
        ) as mock_conn_retrieve_by_req_id, async_mock.patch.object(
            ConnRecord, "retrieve_by_id", async_mock.CoroutineMock()
        ) as mock_conn_retrieve_by_id, async_mock.patch.object(
            DIDDoc, "deserialize", async_mock.MagicMock()
        ) as mock_did_doc_deser:
            mock_did_doc_deser.return_value = async_mock.MagicMock(
                did=TestConfig.test_did
            )
            mock_conn_retrieve_by_req_id.return_value = async_mock.MagicMock(
                did=TestConfig.test_target_did,
                did_doc_attach=async_mock.MagicMock(
                    data=async_mock.MagicMock(
                        verify=async_mock.CoroutineMock(return_value=True),
                        signed=async_mock.MagicMock(
                            decode=async_mock.MagicMock(
                                return_value=json.dumps({"dummy": "did-doc"})
                            )
                        ),
                    )
                ),
                state=ConnRecord.State.REQUEST.rfc23,
                save=async_mock.CoroutineMock(),
            )
            mock_conn_retrieve_by_id.return_value = async_mock.MagicMock(
                their_did=TestConfig.test_target_did,
                save=async_mock.CoroutineMock(),
            )

            with self.assertRaises(DIDXManagerError):
                await self.manager.accept_response(mock_response, receipt)

    async def test_accept_complete(self):
        mock_complete = async_mock.MagicMock()
        receipt = MessageReceipt(sender_did=TestConfig.test_target_did)

        with async_mock.patch.object(
            ConnRecord, "retrieve_by_request_id", async_mock.CoroutineMock()
        ) as mock_conn_retrieve_by_req_id:
            mock_conn_retrieve_by_req_id.return_value.save = async_mock.CoroutineMock()
            conn_rec = await self.manager.accept_complete(mock_complete, receipt)
            assert ConnRecord.State.get(conn_rec.state) is ConnRecord.State.COMPLETED

    async def test_accept_complete_x_not_found(self):
        mock_complete = async_mock.MagicMock()
        receipt = MessageReceipt(sender_did=TestConfig.test_target_did)

        with async_mock.patch.object(
            ConnRecord, "retrieve_by_request_id", async_mock.CoroutineMock()
        ) as mock_conn_retrieve_by_req_id:
            mock_conn_retrieve_by_req_id.side_effect = StorageNotFoundError()
            with self.assertRaises(DIDXManagerError):
                await self.manager.accept_complete(mock_complete, receipt)

    async def test_create_did_document(self):
        did_info = DIDInfo(
            TestConfig.test_did,
            TestConfig.test_verkey,
            None,
            method=DIDMethod.SOV,
            key_type=KeyType.ED25519,
        )

        mock_conn = async_mock.MagicMock(
            connection_id="dummy",
            inbound_connection_id=None,
            their_did=TestConfig.test_target_did,
            state=ConnRecord.State.COMPLETED.rfc23,
        )

        did_doc = self.make_did_doc(
            did=TestConfig.test_target_did,
            verkey=TestConfig.test_target_verkey,
        )
        for i in range(2):  # first cover store-record, then update-value
            await self.manager.store_did_document(did_doc)

        with async_mock.patch.object(
            ConnRecord, "retrieve_by_id", async_mock.CoroutineMock()
        ) as mock_conn_rec_retrieve_by_id:
            mock_conn_rec_retrieve_by_id.return_value = mock_conn

            did_doc = await self.manager.create_did_document(
                did_info=did_info,
                inbound_connection_id="dummy",
                svc_endpoints=[TestConfig.test_endpoint],
            )

    async def test_create_did_document_not_completed(self):
        did_info = DIDInfo(
            TestConfig.test_did,
            TestConfig.test_verkey,
            None,
            method=DIDMethod.SOV,
            key_type=KeyType.ED25519,
        )

        mock_conn = async_mock.MagicMock(
            connection_id="dummy",
            inbound_connection_id=None,
            their_did=TestConfig.test_target_did,
            state=ConnRecord.State.ABANDONED.rfc23,
        )

        with async_mock.patch.object(
            ConnRecord, "retrieve_by_id", async_mock.CoroutineMock()
        ) as mock_conn_rec_retrieve_by_id:
            mock_conn_rec_retrieve_by_id.return_value = mock_conn

            with self.assertRaises(BaseConnectionManagerError):
                await self.manager.create_did_document(
                    did_info=did_info,
                    inbound_connection_id="dummy",
                    svc_endpoints=[TestConfig.test_endpoint],
                )

    async def test_create_did_document_no_services(self):
        did_info = DIDInfo(
            TestConfig.test_did,
            TestConfig.test_verkey,
            None,
            method=DIDMethod.SOV,
            key_type=KeyType.ED25519,
        )

        mock_conn = async_mock.MagicMock(
            connection_id="dummy",
            inbound_connection_id=None,
            their_did=TestConfig.test_target_did,
            state=ConnRecord.State.COMPLETED.rfc23,
        )

        x_did_doc = self.make_did_doc(
            did=TestConfig.test_target_did, verkey=TestConfig.test_target_verkey
        )
        x_did_doc._service = {}
        for i in range(2):  # first cover store-record, then update-value
            await self.manager.store_did_document(x_did_doc)

        with async_mock.patch.object(
            ConnRecord, "retrieve_by_id", async_mock.CoroutineMock()
        ) as mock_conn_rec_retrieve_by_id:
            mock_conn_rec_retrieve_by_id.return_value = mock_conn

            with self.assertRaises(BaseConnectionManagerError):
                await self.manager.create_did_document(
                    did_info=did_info,
                    inbound_connection_id="dummy",
                    svc_endpoints=[TestConfig.test_endpoint],
                )

    async def test_create_did_document_no_service_endpoint(self):
        did_info = DIDInfo(
            TestConfig.test_did,
            TestConfig.test_verkey,
            None,
            method=DIDMethod.SOV,
            key_type=KeyType.ED25519,
        )

        mock_conn = async_mock.MagicMock(
            connection_id="dummy",
            inbound_connection_id=None,
            their_did=TestConfig.test_target_did,
            state=ConnRecord.State.COMPLETED.rfc23,
        )

        x_did_doc = self.make_did_doc(
            did=TestConfig.test_target_did, verkey=TestConfig.test_target_verkey
        )
        x_did_doc._service = {}
        x_did_doc.set(
            Service(TestConfig.test_target_did, "dummy", "IndyAgent", [], [], "", 0)
        )
        for i in range(2):  # first cover store-record, then update-value
            await self.manager.store_did_document(x_did_doc)

        with async_mock.patch.object(
            ConnRecord, "retrieve_by_id", async_mock.CoroutineMock()
        ) as mock_conn_rec_retrieve_by_id:
            mock_conn_rec_retrieve_by_id.return_value = mock_conn

            with self.assertRaises(BaseConnectionManagerError):
                await self.manager.create_did_document(
                    did_info=did_info,
                    inbound_connection_id="dummy",
                    svc_endpoints=[TestConfig.test_endpoint],
                )

    async def test_create_did_document_no_service_recip_keys(self):
        did_info = DIDInfo(
            TestConfig.test_did,
            TestConfig.test_verkey,
            None,
            method=DIDMethod.SOV,
            key_type=KeyType.ED25519,
        )

        mock_conn = async_mock.MagicMock(
            connection_id="dummy",
            inbound_connection_id=None,
            their_did=TestConfig.test_target_did,
            state=ConnRecord.State.COMPLETED.rfc23,
        )

        x_did_doc = self.make_did_doc(
            did=TestConfig.test_target_did, verkey=TestConfig.test_target_verkey
        )
        x_did_doc._service = {}
        x_did_doc.set(
            Service(
                TestConfig.test_target_did,
                "dummy",
                "IndyAgent",
                [],
                [],
                TestConfig.test_endpoint,
                0,
            )
        )
        for i in range(2):  # first cover store-record, then update-value
            await self.manager.store_did_document(x_did_doc)

        with async_mock.patch.object(
            ConnRecord, "retrieve_by_id", async_mock.CoroutineMock()
        ) as mock_conn_rec_retrieve_by_id:
            mock_conn_rec_retrieve_by_id.return_value = mock_conn

            with self.assertRaises(BaseConnectionManagerError):
                await self.manager.create_did_document(
                    did_info=did_info,
                    inbound_connection_id="dummy",
                    svc_endpoints=[TestConfig.test_endpoint],
                )

    async def test_did_key_storage(self):
        did_info = DIDInfo(
            TestConfig.test_did,
            TestConfig.test_verkey,
            None,
            method=DIDMethod.SOV,
            key_type=KeyType.ED25519,
        )

        did_doc = self.make_did_doc(
            did=TestConfig.test_target_did, verkey=TestConfig.test_target_verkey
        )

        await self.manager.add_key_for_did(
            did=TestConfig.test_target_did, key=TestConfig.test_target_verkey
        )

        did = await self.manager.find_did_for_key(key=TestConfig.test_target_verkey)
        assert did == TestConfig.test_target_did
        await self.manager.remove_keys_for_did(TestConfig.test_target_did)

    async def test_diddoc_connection_targets_diddoc(self):
        did_doc = self.make_did_doc(
            TestConfig.test_target_did,
            TestConfig.test_target_verkey,
        )
        targets = self.manager.diddoc_connection_targets(
            did_doc,
            TestConfig.test_verkey,
        )
        assert isinstance(targets[0], ConnectionTarget)

    async def test_diddoc_connection_targets_diddoc_underspecified(self):
        with self.assertRaises(BaseConnectionManagerError):
            self.manager.diddoc_connection_targets(None, TestConfig.test_verkey)

        x_did_doc = DIDDoc(did=None)
        with self.assertRaises(BaseConnectionManagerError):
            self.manager.diddoc_connection_targets(x_did_doc, TestConfig.test_verkey)

        x_did_doc = self.make_did_doc(
            did=TestConfig.test_target_did, verkey=TestConfig.test_target_verkey
        )
        x_did_doc._service = {}
        with self.assertRaises(BaseConnectionManagerError):
            self.manager.diddoc_connection_targets(x_did_doc, TestConfig.test_verkey)
