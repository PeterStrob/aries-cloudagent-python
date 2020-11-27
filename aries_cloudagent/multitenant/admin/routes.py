"""Multitenant admin routes."""

from marshmallow import fields, Schema, validate, validates_schema, ValidationError
from aiohttp import web
from aiohttp_apispec import docs, request_schema, match_info_schema

from ...messaging.valid import UUIDFour
from ...messaging.models.openapi import OpenAPISchema
from ...storage.error import StorageNotFoundError
from ...wallet.provider import WalletProvider
from ...wallet.models.wallet_record import WalletRecord
from ...core.error import BaseError
from ..manager import MultitenantManager
from ..error import WalletKeyMissingError


def format_wallet_record(wallet_record: WalletRecord):
    """Serialize a WalletRecord object."""

    wallet_info = {
        "wallet_id": wallet_record.wallet_record_id,
        "wallet_type": wallet_record.wallet_config.get("type"),
        "wallet_name": wallet_record.wallet_name,
        "created_at": wallet_record.created_at,
        "updated_at": wallet_record.updated_at,
    }

    return wallet_info


class WalletIdMatchInfoSchema(OpenAPISchema):
    """Path parameters and validators for request taking wallet id."""

    wallet_id = fields.Str(
        description="Subwallet identifier", required=True, example=UUIDFour.EXAMPLE
    )


class CreateWalletRequestSchema(Schema):
    """Request schema for adding a new wallet which will be registered by the agent."""

    wallet_name = fields.Str(description="Wallet name", example="MyNewWallet")

    wallet_key = fields.Str(
        description="Master key used for key derivation.", example="MySecretKey123"
    )

    # MTODO: add seed
    # seed = fields.Str(
    #     description="Seed used for did derivation - 32 bytes.",
    #     example="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    # )

    wallet_type = fields.Str(
        description="Type of the wallet to create",
        example="indy",
        default="basic",
        validate=validate.OneOf(
            [wallet_type for wallet_type in WalletProvider.WALLET_TYPES]
        ),
    )

    @validates_schema
    def validate_fields(self, data, **kwargs):
        """
        Validate schema fields.

        Args:
            data: The data to validate

        Raises:
            ValidationError: If any of the fields do not validate

        """

        if data.get("wallet_type") == "indy":
            for field in ("wallet_key", "wallet_name"):
                if field not in data:
                    raise ValidationError("Missing required field", field)


class RemoveWalletRequestSchema(Schema):
    """Request schema for removing a wallet."""

    wallet_key = fields.Str(
        description="Master key used for key derivation.", example="MySecretKey123"
    )


class CreateWalletTokenRequestSchema(Schema):
    """Request schema for creating a wallet token."""

    wallet_key = fields.Str(
        description="Master key used for key derivation.", example="MySecretKey123"
    )


@docs(tags=["multitenancy"], summary="List all subwallets")
# MTODO: wallet_list response schema
async def wallet_list(request: web.BaseRequest):
    """
    Request handler for listing all internal subwallets.

    Args:
        request: aiohttp request object
    """

    context = request["context"]

    try:
        records = await WalletRecord.query(context)
        results = [format_wallet_record(record) for record in records]
    except StorageNotFoundError:
        raise web.HTTPNotFound()
    return web.json_response({"results": results})


@docs(tags=["multitenancy"], summary="Get a single subwallet")
@match_info_schema(WalletIdMatchInfoSchema())
# MTODO: wallet_get response schema
async def wallet_get(request: web.BaseRequest):
    """
    Request handler for getting a single subwallet.

    Args:
        request: aiohttp request object

    Raises:
        HTTPNotFound: if wallet_id does not match any known wallets

    """

    context = request["context"]
    wallet_id = request.match_info["wallet_id"]

    try:
        wallet_record = await WalletRecord.retrieve_by_id(context, wallet_id)
        result = format_wallet_record(wallet_record)
    except StorageNotFoundError:
        raise web.HTTPNotFound()
    return web.json_response(result)


@docs(tags=["multitenancy"], summary="Create a subwallet")
@request_schema(CreateWalletRequestSchema)
# MTODO: wallet_create Response schema
async def wallet_create(request: web.BaseRequest):
    """
    Request handler for adding a new subwallet for handling by the agent.

    Args:
        request: aiohttp request object
    """

    context = request["context"]
    body = await request.json()

    # MTODO: make mode variable. Either trough setting or body parameter
    key_management_mode = WalletRecord.MODE_MANAGED  # body.get("key_management_mode")
    wallet_name = body.get("wallet_name")
    wallet_key = body.get("wallet_key")

    wallet_config = {
        "type": body.get("wallet_type"),
        "name": wallet_name,
        "key": wallet_key,
    }

    try:
        multitenant_manager = MultitenantManager(context)

        wallet_record = await multitenant_manager.create_wallet(
            wallet_config,
            key_management_mode,
        )

        token = await multitenant_manager.create_auth_token(wallet_record, wallet_key)
    except BaseError as err:
        raise web.HTTPBadRequest(reason=err.roll_up) from err

    result = {
        **format_wallet_record(wallet_record),
        "token": token,
    }
    return web.json_response(result)


@docs(tags=["multitenancy"], summary="Get auth token for a subwallet")
@request_schema(CreateWalletTokenRequestSchema)
# MTODO: wallet_create_token Response schema
async def wallet_create_token(request: web.BaseRequest):
    """
    Request handler for creating an authorization token for a specific subwallet.

    Args:
        request: aiohttp request object
    """

    context = request["context"]
    wallet_id = request.match_info["wallet_id"]
    wallet_key = None

    if request.has_body:
        body = await request.json()
        wallet_key = body.get("wallet_key")

    try:
        multitenant_manager = MultitenantManager(context)
        wallet_record = await WalletRecord.retrieve_by_id(context, wallet_id)

        token = await multitenant_manager.create_auth_token(wallet_record, wallet_key)
    except StorageNotFoundError:
        raise web.HTTPNotFound()
    except WalletKeyMissingError as e:
        raise web.HTTPUnauthorized(e.roll_up) from e

    return web.json_response({"token": token})


@docs(
    tags=["multitenancy"],
    summary="Remove a subwallet",
)
@match_info_schema(WalletIdMatchInfoSchema())
@request_schema(RemoveWalletRequestSchema)
# MTODO: wallet_remove response schema
async def wallet_remove(request: web.BaseRequest):
    """
    Request handler to remove a subwallet from agent and storage.

    Args:
        request: aiohttp request object.

    """

    context = request["context"]
    wallet_id = request.match_info["wallet_id"]
    wallet_key = None

    if request.has_body:
        body = await request.json()
        wallet_key = body.get("wallet_key")

    try:
        multitenant_manager = MultitenantManager(context)
        await multitenant_manager.remove_wallet(wallet_id, wallet_key)
    except StorageNotFoundError:
        raise web.HTTPNotFound()
    except WalletKeyMissingError as e:
        raise web.HTTPUnauthorized(e.message)

    return web.json_response({})


# MTODO: add wallet import route
# MTODO: add wallet export route
# MTODO: add rotate wallet key route


async def register(app: web.Application):
    """Register routes."""

    app.add_routes(
        [
            web.get("/multitenancy/wallets", wallet_list, allow_head=False),
            web.post("/multitenancy/wallet", wallet_create),
            web.get("/multitenancy/wallet/{wallet_id}", wallet_get, allow_head=False),
            web.post("/multitenancy/wallet/{wallet_id}/token", wallet_create_token),
            web.post("/multitenancy/wallet/{wallet_id}/remove", wallet_remove),
        ]
    )


def post_process_routes(app: web.Application):
    """Amend swagger API."""

    # Add top-level tags description
    if "tags" not in app._state["swagger_dict"]:
        app._state["swagger_dict"]["tags"] = []
    app._state["swagger_dict"]["tags"].append(
        {"name": "multitenancy", "description": "Multitenant wallet management"}
    )
