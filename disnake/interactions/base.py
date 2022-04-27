# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional, Tuple, Union, cast, overload

from .. import utils
from ..app_commands import OptionChoice
from ..channel import ChannelType, PartialMessageable
from ..enums import InteractionResponseType, InteractionType, Locale, WebhookType, try_enum
from ..errors import (
    HTTPException,
    InteractionNotEditable,
    InteractionNotResponded,
    InteractionResponded,
    InteractionTimedOut,
    ModalChainNotSupported,
    NotFound,
)
from ..flags import MessageFlags
from ..guild import Guild
from ..i18n import Localized
from ..member import Member
from ..message import Attachment, Message
from ..object import Object
from ..permissions import Permissions
from ..ui.action_row import components_to_dict
from ..user import ClientUser, User
from ..webhook.async_ import Webhook, async_context, handle_message_parameters

__all__ = (
    "Interaction",
    "InteractionMessage",
    "InteractionResponse",
)

if TYPE_CHECKING:
    from datetime import datetime

    from aiohttp import ClientSession

    from ..app_commands import Choices
    from ..channel import CategoryChannel, StageChannel, TextChannel, VoiceChannel
    from ..client import Client
    from ..embeds import Embed
    from ..ext.commands import AutoShardedBot, Bot
    from ..file import File
    from ..guild import GuildMessageable
    from ..mentions import AllowedMentions
    from ..state import ConnectionState
    from ..threads import Thread
    from ..types.components import Modal as ModalPayload
    from ..types.interactions import (
        ApplicationCommandOptionChoice as ApplicationCommandOptionChoicePayload,
        Interaction as InteractionPayload,
    )
    from ..ui.action_row import Components, MessageUIComponent, ModalUIComponent
    from ..ui.modal import Modal
    from ..ui.view import View
    from .message import MessageInteraction
    from .modal import ModalInteraction

    InteractionChannel = Union[
        VoiceChannel,
        StageChannel,
        TextChannel,
        CategoryChannel,
        Thread,
        PartialMessageable,
    ]

    AnyBot = Union[Bot, AutoShardedBot]

MISSING: Any = utils.MISSING


class _ResponseLock:
    __slots__ = (
        "interaction",
        "_condition",
    )

    def __init__(self, interaction: "Interaction") -> None:
        self.interaction = interaction
        self._condition = asyncio.Condition()

    async def __aenter__(self):
        # when we acquire this manager, we are attempting to make a request
        # the problem is we don't know if the request will success or not
        # to get around this, we wait and see if it was successful.
        # if it was successful, we raise an InteractionResponded exception
        if not self._condition.locked():
            await self._condition.acquire()
            return

        await self._condition.acquire()
        if self.interaction.response._responded:
            raise InteractionResponded(self.interaction)

    async def __aexit__(self, exc_type, exc, tb):
        # we are no longer making a request so we release the lock
        # however, we only want to set responded to True if it was success
        # eg did not raise an Exception
        self._condition.release()
        if exc_type is not None:
            return
        self.interaction.response._responded = True

    def is_responding(self):
        return self._condition.locked()

    async def wait(self):
        if self._condition.locked():
            return await self._condition.wait()
        return True


class Interaction:
    """A base class representing a user-initiated Discord interaction.

    An interaction happens when a user performs an action that the client needs to
    be notified of. Current examples are application commands and components.

    .. versionadded:: 2.0

    Attributes
    ----------
    data: Mapping[:class:`str`, Any]
        The interaction's raw data. This might be replaced with a more processed version in subclasses.
    id: :class:`int`
        The interaction's ID.
    type: :class:`InteractionType`
        The interaction's type.
    application_id: :class:`int`
        The application ID that the interaction was for.
    guild_id: Optional[:class:`int`]
        The guild ID the interaction was sent from.
    guild_locale: Optional[:class:`Locale`]
        The selected language of the interaction's guild.
        This value is only meaningful in guilds with ``COMMUNITY`` feature and receives a default value otherwise.
        If the interaction was in a DM, then this value is ``None``.

        .. versionadded:: 2.4

        .. versionchanged:: 2.5
            Changed to :class:`Locale` instead of :class:`str`.

    channel_id: :class:`int`
        The channel ID the interaction was sent from.
    author: Union[:class:`User`, :class:`Member`]
        The user or member that sent the interaction.
    locale: :class:`Locale`
        The selected language of the interaction's author.

        .. versionadded:: 2.4

        .. versionchanged:: 2.5
            Changed to :class:`Locale` instead of :class:`str`.

    token: :class:`str`
        The token to continue the interaction. These are valid for 15 minutes.
    client: :class:`Client`
        The interaction client.
    """

    __slots__: Tuple[str, ...] = (
        "data",
        "id",
        "type",
        "guild_id",
        "channel_id",
        "application_id",
        "author",
        "token",
        "version",
        "locale",
        "guild_locale",
        "client",
        "_app_permissions",
        "_permissions",
        "_state",
        "_session",
        "_original_response",
        "_response_lock",
        "_cs_response",
        "_cs_followup",
        "_cs_channel",
        "_cs_me",
        "_cs_expires_at",
    )

    def __init__(self, *, data: InteractionPayload, state: ConnectionState):
        self.data: Mapping[str, Any] = data.get("data") or {}
        self._state: ConnectionState = state
        # TODO: Maybe use a unique session
        self._session: ClientSession = state.http._HTTPClient__session  # type: ignore
        self.client: Client = state._get_client()
        self._original_response: Optional[InteractionMessage] = None
        self._response_lock = _ResponseLock(self)

        self.id: int = int(data["id"])
        self.type: InteractionType = try_enum(InteractionType, data["type"])
        self.token: str = data["token"]
        self.version: int = data["version"]
        self.application_id: int = int(data["application_id"])
        self._app_permissions: int = int(data.get("app_permissions", 0))

        self.channel_id: int = int(data["channel_id"])
        self.guild_id: Optional[int] = utils._get_as_snowflake(data, "guild_id")
        self.locale: Locale = try_enum(Locale, data["locale"])
        guild_locale = data.get("guild_locale")
        self.guild_locale: Optional[Locale] = (
            try_enum(Locale, guild_locale) if guild_locale else None
        )
        # one of user and member will always exist
        self.author: Union[User, Member] = MISSING
        self._permissions = None

        if self.guild_id and (member := data.get("member")):
            guild: Guild = self.guild or Object(id=self.guild_id)  # type: ignore
            self.author = (
                isinstance(guild, Guild)
                and guild.get_member(int(member["user"]["id"]))
                or Member(state=self._state, guild=guild, data=member)
            )
            self._permissions = int(member.get("permissions", 0))
        elif user := data.get("user"):
            self.author = self._state.store_user(user)

    @property
    def bot(self) -> AnyBot:
        """:class:`~disnake.ext.commands.Bot`: The bot handling the interaction.

        Only applicable when used with :class:`~disnake.ext.commands.Bot`.
        This is an alias for :attr:`.client`.
        """
        return self.client  # type: ignore

    @property
    def created_at(self) -> datetime:
        """:class:`datetime.datetime`: Returns the interaction's creation time in UTC."""
        return utils.snowflake_time(self.id)

    @property
    def user(self) -> Union[User, Member]:
        """Union[:class:`.User`, :class:`.Member`]: The user or member that sent the interaction.
        There is an alias for this named :attr:`author`."""
        return self.author

    @property
    def guild(self) -> Optional[Guild]:
        """Optional[:class:`Guild`]: The guild the interaction was sent from."""
        return self._state._get_guild(self.guild_id)

    @utils.cached_slot_property("_cs_me")
    def me(self) -> Union[Member, ClientUser]:
        """Union[:class:`.Member`, :class:`.ClientUser`]:
        Similar to :attr:`.Guild.me` except it may return the :class:`.ClientUser` in private message contexts.
        """
        if self.guild is None:
            return None if self.bot is None else self.bot.user  # type: ignore
        return self.guild.me

    @utils.cached_slot_property("_cs_channel")
    def channel(self) -> Union[GuildMessageable, PartialMessageable]:
        """Union[:class:`abc.GuildChannel`, :class:`Thread`, :class:`PartialMessageable`]: The channel the interaction was sent from.

        Note that due to a Discord limitation, threads that the bot cannot access and DM channels
        are not resolved since there is no data to complete them.
        These are :class:`PartialMessageable` instead.

        If you want to compute the interaction author's or bot's permissions in the channel,
        consider using :attr:`permissions` or :attr:`app_permissions` instead.
        """
        guild = self.guild
        channel = guild and guild._resolve_channel(self.channel_id)
        if channel is None:
            # could be a thread channel in a guild, or a DM channel
            type = None if self.guild_id is not None else ChannelType.private
            return PartialMessageable(state=self._state, id=self.channel_id, type=type)
        return channel  # type: ignore

    @property
    def permissions(self) -> Permissions:
        """:class:`Permissions`: The resolved permissions of the member in the channel, including overwrites.

        In a guild context, this is provided directly by Discord.

        In a non-guild context this will be an instance of :meth:`Permissions.private_channel`.
        """
        if self._permissions is not None:
            return Permissions(self._permissions)
        return Permissions.private_channel()

    @property
    def app_permissions(self) -> Permissions:
        """:class:`Permissions`: The resolved permissions of the bot in the channel, including overwrites.

        In a guild context, this is provided directly by Discord.

        In a non-guild context this will be an instance of :meth:`Permissions.private_channel`.

        .. versionadded:: 2.6
        """
        if self.guild_id:
            return Permissions(self._app_permissions)
        return Permissions.private_channel()

    @utils.cached_slot_property("_cs_response")
    def response(self) -> InteractionResponse:
        """:class:`InteractionResponse`: Returns an object responsible for handling responding to the interaction.

        A response can only be done once. If secondary messages need to be sent, consider using :attr:`followup`
        instead.
        """
        return InteractionResponse(self)

    @utils.cached_slot_property("_cs_followup")
    def followup(self) -> Webhook:
        """:class:`Webhook`: Returns the follow up webhook for follow up interactions."""
        payload = {
            "id": self.application_id,
            "type": WebhookType.application.value,
            "token": self.token,
        }
        return Webhook.from_state(data=payload, state=self._state)

    @utils.cached_slot_property("_cs_expires_at")
    def expires_at(self) -> datetime:
        """:class:`datetime.datetime`: Returns the interaction's expiration time in UTC.

        This is exactly 15 minutes after the interaction was created.

        .. versionadded:: 2.5
        """
        return self.created_at + timedelta(minutes=15)

    def is_expired(self) -> bool:
        """Whether the interaction can still be used to make requests to Discord.

        This does not take into account the 3 second limit for the initial response.

        .. versionadded:: 2.5

        :return type: :class:`bool`
        """
        return self.expires_at <= utils.utcnow()

    async def original_response(self) -> InteractionMessage:
        """|coro|

        Fetches the original interaction response message associated with the interaction.

        Here is a table with response types and their associated original response:

        .. csv-table::
            :header: "Response type", "Original response"

            :meth:`InteractionResponse.send_message`, "The message you sent"
            :meth:`InteractionResponse.edit_message`, "The message you edited"
            :meth:`InteractionResponse.defer`, "The message with thinking state (bot is thinking...)"
            "Other response types", "None"

        Repeated calls to this will return a cached value.

        .. versionchanged:: 2.6

            This function was renamed from ``original_message``.

        Raises
        ------
        HTTPException
            Fetching the original response message failed.

        Returns
        -------
        InteractionMessage
            The original interaction response message.
        """
        if self._original_response is not None:
            return self._original_response

        adapter = async_context.get()
        data = await adapter.get_original_interaction_response(
            application_id=self.application_id,
            token=self.token,
            session=self._session,
        )
        state = _InteractionMessageState(self, self._state)
        message = InteractionMessage(state=state, channel=self.channel, data=data)  # type: ignore
        self._original_response = message
        return message

    async def edit_original_response(
        self,
        content: Optional[str] = MISSING,
        *,
        embed: Optional[Embed] = MISSING,
        embeds: List[Embed] = MISSING,
        file: File = MISSING,
        files: List[File] = MISSING,
        attachments: Optional[List[Attachment]] = MISSING,
        view: Optional[View] = MISSING,
        components: Optional[Components[MessageUIComponent]] = MISSING,
        allowed_mentions: Optional[AllowedMentions] = None,
    ) -> InteractionMessage:
        """|coro|

        Edits the original, previously sent interaction response message.

        This is a lower level interface to :meth:`InteractionMessage.edit` in case
        you do not want to fetch the message and save an HTTP request.

        This method is also the only way to edit the original response if
        the message sent was ephemeral.

        .. note::
            If the original response message has embeds with images that were created from local files
            (using the ``file`` parameter with :meth:`Embed.set_image` or :meth:`Embed.set_thumbnail`),
            those images will be removed if the message's attachments are edited in any way
            (i.e. by setting ``file``/``files``/``attachments``, or adding an embed with local files).

        .. versionchanged:: 2.6

            This function was renamed from ``edit_original_message``.

        Parameters
        ----------
        content: Optional[:class:`str`]
            The content to edit the message with, or ``None`` to clear it.
        embed: Optional[:class:`Embed`]
            The new embed to replace the original with. This cannot be mixed with the
            ``embeds`` parameter.
            Could be ``None`` to remove the embed.
        embeds: List[:class:`Embed`]
            The new embeds to replace the original with. Must be a maximum of 10.
            This cannot be mixed with the ``embed`` parameter.
            To remove all embeds ``[]`` should be passed.
        file: :class:`File`
            The file to upload. This cannot be mixed with the ``files`` parameter.
            Files will be appended to the message, see the ``attachments`` parameter
            to remove/replace existing files.
        files: List[:class:`File`]
            A list of files to upload. This cannot be mixed with the ``file`` parameter.
            Files will be appended to the message, see the ``attachments`` parameter
            to remove/replace existing files.
        attachments: Optional[List[:class:`Attachment`]]
            A list of attachments to keep in the message.
            If ``[]`` or ``None`` is passed then all existing attachments are removed.
            Keeps existing attachments if not provided.

            .. versionadded:: 2.2

            .. versionchanged:: 2.5
                Supports passing ``None`` to clear attachments.

        view: Optional[:class:`~disnake.ui.View`]
            The updated view to update this message with. This cannot be mixed with ``components``.
            If ``None`` is passed then the view is removed.
        components: Optional[|components_type|]
            A list of components to update this message with. This cannot be mixed with ``view``.
            If ``None`` is passed then the components are removed.

            .. versionadded:: 2.4

        allowed_mentions: :class:`AllowedMentions`
            Controls the mentions being processed in this message.
            See :meth:`.abc.Messageable.send` for more information.

        Raises
        ------
        HTTPException
            Editing the message failed.
        Forbidden
            Edited a message that is not yours.
        TypeError
            You specified both ``embed`` and ``embeds`` or ``file`` and ``files``
        ValueError
            The length of ``embeds`` was invalid.

        Returns
        -------
        :class:`InteractionMessage`
            The newly edited message.
        """
        # if no attachment list was provided but we're uploading new files,
        # use current attachments as the base
        if attachments is MISSING and (file or files):
            attachments = (await self.original_response()).attachments

        previous_mentions: Optional[AllowedMentions] = self._state.allowed_mentions
        params = handle_message_parameters(
            content=content,
            file=file,
            files=files,
            attachments=attachments,
            embed=embed,
            embeds=embeds,
            view=view,
            components=components,
            allowed_mentions=allowed_mentions,
            previous_allowed_mentions=previous_mentions,
        )
        adapter = async_context.get()
        try:
            data = await adapter.edit_original_interaction_response(
                self.application_id,
                self.token,
                session=self._session,
                payload=params.payload,
                multipart=params.multipart,
                files=params.files,
            )
        except NotFound as e:
            if e.code == 10015:
                raise InteractionNotResponded(self) from e
            raise
        finally:
            if params.files:
                for f in params.files:
                    f.close()

        # The message channel types should always match
        state = _InteractionMessageState(self, self._state)
        message = InteractionMessage(state=state, channel=self.channel, data=data)  # type: ignore
        if view and not view.is_finished():
            self._state.store_view(view, message.id)
        return message

    async def delete_original_response(self, *, delay: Optional[float] = None) -> None:
        """|coro|

        Deletes the original interaction response message.

        This is a lower level interface to :meth:`InteractionMessage.delete` in case
        you do not want to fetch the message and save an HTTP request.

        .. versionchanged:: 2.6

            This function was renamed from ``delete_original_message``.

        Parameters
        ----------
        delay: :class:`float`
            If provided, the number of seconds to wait in the background
            before deleting the original response message. If the deletion fails,
            then it is silently ignored.

        Raises
        ------
        HTTPException
            Deleting the message failed.
        Forbidden
            Deleted a message that is not yours.
        """
        adapter = async_context.get()
        deleter = adapter.delete_original_interaction_response(
            self.application_id,
            self.token,
            session=self._session,
        )

        if delay is not None:

            async def delete(delay: float):
                await asyncio.sleep(delay)
                try:
                    await deleter
                except HTTPException:
                    pass

            asyncio.create_task(delete(delay))
            return

        try:
            await deleter
        except NotFound as e:
            if e.code == 10015:
                raise InteractionNotResponded(self) from e
            raise

    # legacy namings
    # these MAY begin a deprecation warning in 2.7 but SHOULD have a deprecation version in 2.8
    original_message = original_response
    edit_original_message = edit_original_response
    delete_original_message = delete_original_response

    async def send(
        self,
        content: Optional[str] = None,
        *,
        embed: Embed = MISSING,
        embeds: List[Embed] = MISSING,
        file: File = MISSING,
        files: List[File] = MISSING,
        allowed_mentions: AllowedMentions = MISSING,
        view: View = MISSING,
        components: Components[MessageUIComponent] = MISSING,
        tts: bool = False,
        ephemeral: bool = False,
        suppress_embeds: bool = False,
        delete_after: float = MISSING,
    ) -> None:
        """|coro|

        Sends a message using either :meth:`response.send_message <InteractionResponse.send_message>`
        or :meth:`followup.send <Webhook.send>`.

        If the interaction hasn't been responded to yet, this method will call :meth:`response.send_message <InteractionResponse.send_message>`.
        Otherwise, it will call :meth:`followup.send <Webhook.send>`.

        .. note::
            This method does not return a :class:`Message` object. If you need a message object,
            use :meth:`original_response` to fetch it, or use :meth:`followup.send <Webhook.send>`
            directly instead of this method if you're sending a followup message.

        Parameters
        ----------
        content: Optional[:class:`str`]
            The content of the message to send.
        embed: :class:`Embed`
            The rich embed for the content to send. This cannot be mixed with the
            ``embeds`` parameter.
        embeds: List[:class:`Embed`]
            A list of embeds to send with the content. Must be a maximum of 10.
            This cannot be mixed with the ``embed`` parameter.
        file: :class:`File`
            The file to upload. This cannot be mixed with the ``files`` parameter.
        files: List[:class:`File`]
            A list of files to upload. Must be a maximum of 10.
            This cannot be mixed with the ``file`` parameter.
        allowed_mentions: :class:`AllowedMentions`
            Controls the mentions being processed in this message. If this is
            passed, then the object is merged with :attr:`Client.allowed_mentions <disnake.Client.allowed_mentions>`.
            The merging behaviour only overrides attributes that have been explicitly passed
            to the object, otherwise it uses the attributes set in :attr:`Client.allowed_mentions <disnake.Client.allowed_mentions>`.
            If no object is passed at all then the defaults given by :attr:`Client.allowed_mentions <disnake.Client.allowed_mentions>`
            are used instead.
        tts: :class:`bool`
            Whether the message should be sent using text-to-speech.
        view: :class:`disnake.ui.View`
            The view to send with the message. This cannot be mixed with ``components``.
        components: |components_type|
            A list of components to send with the message. This cannot be mixed with ``view``.

            .. versionadded:: 2.4

        ephemeral: :class:`bool`
            Whether the message should only be visible to the user who started the interaction.
            If a view is sent with an ephemeral message and it has no timeout set then the timeout
            is set to 15 minutes.
        suppress_embeds: :class:`bool`
            Whether to suppress embeds for the message. This hides
            all embeds from the UI if set to ``True``.

            .. versionadded:: 2.5

        delete_after: :class:`float`
            If provided, the number of seconds to wait in the background
            before deleting the message we just sent. If the deletion fails,
            then it is silently ignored.

        Raises
        ------
        HTTPException
            Sending the message failed.
        TypeError
            You specified both ``embed`` and ``embeds``.
        ValueError
            The length of ``embeds`` was invalid.
        """
        # this is done before determining which sender to use
        # if we are currently responding we should use the followup
        # but there is a chance the response will fail and therefore
        # we should send the response
        if self._response_lock.is_responding():
            await self._response_lock.wait()

        if self.response._response_type is not None:
            sender = self.followup.send
        else:
            sender = self.response.send_message
        await sender(
            content=content,
            embed=embed,
            embeds=embeds,
            file=file,
            files=files,
            allowed_mentions=allowed_mentions,
            view=view,
            components=components,
            tts=tts,
            ephemeral=ephemeral,
            suppress_embeds=suppress_embeds,
            delete_after=delete_after,
        )


class InteractionResponse:
    """Represents a Discord interaction response.

    This type can be accessed through :attr:`Interaction.response`.

    .. versionadded:: 2.0
    """

    __slots__: Tuple[str, ...] = (
        "_parent",
        "_response_type",
    )

    def __init__(self, parent: Interaction):
        self._parent: Interaction = parent
        self._response_type: Optional[InteractionResponseType] = None

    @property
    def type(self) -> Optional[InteractionResponseType]:
        """Optional[:class:`InteractionResponseType`]: If a response was successfully made, this is the type of the response.

        .. versionadded:: 2.6
        """
        return self._response_type

    def is_done(self) -> bool:
        """Whether an interaction response has been done before.

        An interaction can only be responded to once.

        :return type: :class:`bool`
        """
        return self._response_type is not None

    async def defer(
        self,
        *,
        with_message: bool = MISSING,
        ephemeral: bool = MISSING,
    ) -> None:
        """|coro|

        Defers the interaction response.

        This is typically used when the interaction is acknowledged
        and a secondary action will be done later.

        .. versionchanged:: 2.5

            Raises :exc:`TypeError` when an interaction cannot be deferred.

        Parameters
        ----------
        with_message: :class:`bool`
            Whether the response will be a separate message with thinking state (bot is thinking...).
            This only applies to interactions of type :attr:`InteractionType.component`
            (default ``False``) and :attr:`InteractionType.modal_submit` (default ``True``).

            ``True`` corresponds to a :attr:`~InteractionResponseType.deferred_channel_message` response type,
            while ``False`` corresponds to :attr:`~InteractionResponseType.deferred_message_update`.

            .. note::
                Responses to interactions of type :attr:`InteractionType.application_command` must
                defer using a message, i.e. this will effectively always be ``True`` for those.

            .. versionadded:: 2.4

            .. versionchanged:: 2.6
                Added support for setting this to ``False`` in modal interactions.

        ephemeral: :class:`bool`
            Whether the deferred message will eventually be ephemeral.
            This applies to interactions of type :attr:`InteractionType.application_command`,
            or when the ``with_message`` parameter is ``True``.

            Defaults to ``False``.

        Raises
        ------
        HTTPException
            Deferring the interaction failed.
        InteractionResponded
            This interaction has already been responded to before.
        TypeError
            This interaction cannot be deferred.
        """
        if self._response_type is not None:
            raise InteractionResponded(self._parent)

        defer_type: Optional[InteractionResponseType] = None
        data: Dict[str, Any] = {}
        parent = self._parent

        if parent.type is InteractionType.application_command:
            defer_type = InteractionResponseType.deferred_channel_message
        elif parent.type in (InteractionType.component, InteractionType.modal_submit):
            # if not provided, set default based on interaction type
            # (true for modal_submit, false for component)
            if with_message is MISSING:
                with_message = parent.type is InteractionType.modal_submit

            if with_message:
                defer_type = InteractionResponseType.deferred_channel_message
            else:
                defer_type = InteractionResponseType.deferred_message_update
        else:
            raise TypeError(
                "This interaction must be of type 'application_command', 'modal_submit', or 'component' in order to defer."
            )

        if defer_type is InteractionResponseType.deferred_channel_message:
            # we only want to set flags if we are sending a message
            data["flags"] = 0
            if ephemeral:
                data["flags"] |= MessageFlags.ephemeral.flag

        adapter = async_context.get()
        async with parent._response_lock:
            await adapter.create_interaction_response(
                parent.id,
                parent.token,
                session=parent._session,
                type=defer_type.value,
                data=data or None,
            )
            self._response_type = defer_type

    async def pong(self) -> None:
        """|coro|

        Pongs the ping interaction.

        This should rarely be used.

        Raises
        ------
        HTTPException
            Ponging the interaction failed.
        InteractionResponded
            This interaction has already been responded to before.
        """
        if self._response_type is not None:
            raise InteractionResponded(self._parent)

        parent = self._parent
        if parent.type is InteractionType.ping:
            adapter = async_context.get()
            response_type = InteractionResponseType.pong
            async with parent._response_lock:
                await adapter.create_interaction_response(
                    parent.id,
                    parent.token,
                    session=parent._session,
                    type=response_type.value,
                )
                self._response_type = response_type

    async def send_message(
        self,
        content: Optional[str] = None,
        *,
        embed: Embed = MISSING,
        embeds: List[Embed] = MISSING,
        file: File = MISSING,
        files: List[File] = MISSING,
        allowed_mentions: AllowedMentions = MISSING,
        view: View = MISSING,
        components: Components[MessageUIComponent] = MISSING,
        tts: bool = False,
        ephemeral: bool = False,
        suppress_embeds: bool = False,
        delete_after: float = MISSING,
    ) -> None:
        """|coro|

        Responds to this interaction by sending a message.

        Parameters
        ----------
        content: Optional[:class:`str`]
            The content of the message to send.
        embed: :class:`Embed`
            The rich embed for the content to send. This cannot be mixed with the
            ``embeds`` parameter.
        embeds: List[:class:`Embed`]
            A list of embeds to send with the content. Must be a maximum of 10.
            This cannot be mixed with the ``embed`` parameter.
        file: :class:`File`
            The file to upload. This cannot be mixed with the ``files`` parameter.
        files: List[:class:`File`]
            A list of files to upload. Must be a maximum of 10.
            This cannot be mixed with the ``file`` parameter.
        allowed_mentions: :class:`AllowedMentions`
            Controls the mentions being processed in this message.
        view: :class:`disnake.ui.View`
            The view to send with the message. This cannot be mixed with ``components``.
        components: |components_type|
            A list of components to send with the message. This cannot be mixed with ``view``.

            .. versionadded:: 2.4

        tts: :class:`bool`
            Whether the message should be sent using text-to-speech.
        ephemeral: :class:`bool`
            Whether the message should only be visible to the user who started the interaction.
            If a view is sent with an ephemeral message and it has no timeout set then the timeout
            is set to 15 minutes.
        delete_after: :class:`float`
            If provided, the number of seconds to wait in the background
            before deleting the message we just sent. If the deletion fails,
            then it is silently ignored.

        suppress_embeds: :class:`bool`
            Whether to suppress embeds for the message. This hides
            all embeds from the UI if set to ``True``.

            .. versionadded:: 2.5

        Raises
        ------
        HTTPException
            Sending the message failed.
        TypeError
            You specified both ``embed`` and ``embeds``.
        ValueError
            The length of ``embeds`` was invalid.
        InteractionResponded
            This interaction has already been responded to before.
        """
        if self._response_type is not None:
            raise InteractionResponded(self._parent)

        payload: Dict[str, Any] = {
            "tts": tts,
        }

        if delete_after is not MISSING and ephemeral:
            raise ValueError("ephemeral messages can not be deleted via endpoints")

        if embed is not MISSING and embeds is not MISSING:
            raise TypeError("cannot mix embed and embeds keyword arguments")

        if file is not MISSING and files is not MISSING:
            raise TypeError("cannot mix file and files keyword arguments")

        if view is not MISSING and components is not MISSING:
            raise TypeError("cannot mix view and components keyword arguments")

        if file is not MISSING:
            files = [file]

        if embed is not MISSING:
            embeds = [embed]

        if embeds:
            if len(embeds) > 10:
                raise ValueError("embeds cannot exceed maximum of 10 elements")
            payload["embeds"] = [e.to_dict() for e in embeds]
            for embed in embeds:
                if embed._files:
                    files = files or []
                    files.extend(embed._files.values())

        if files is not MISSING and len(files) > 10:
            raise ValueError("files cannot exceed maximum of 10 elements")

        previous_mentions: Optional[AllowedMentions] = getattr(
            self._parent._state, "allowed_mentions", None
        )
        if allowed_mentions:
            if previous_mentions is not None:
                payload["allowed_mentions"] = previous_mentions.merge(allowed_mentions).to_dict()
            else:
                payload["allowed_mentions"] = allowed_mentions.to_dict()
        elif previous_mentions is not None:
            payload["allowed_mentions"] = previous_mentions.to_dict()

        if content is not None:
            payload["content"] = str(content)

        payload["flags"] = 0
        if suppress_embeds:
            payload["flags"] |= MessageFlags.suppress_embeds.flag
        if ephemeral:
            payload["flags"] |= MessageFlags.ephemeral.flag

        if view is not MISSING:
            payload["components"] = view.to_components()

        if components is not MISSING:
            payload["components"] = components_to_dict(components)

        parent = self._parent
        adapter = async_context.get()
        response_type = InteractionResponseType.channel_message
        try:
            async with parent._response_lock:
                await adapter.create_interaction_response(
                    parent.id,
                    parent.token,
                    session=parent._session,
                    type=response_type.value,
                    data=payload,
                    files=files or None,
                )
                self._response_type = response_type
        except NotFound as e:
            if e.code == 10062:
                raise InteractionTimedOut(self._parent) from e
            raise
        finally:
            if files:
                for f in files:
                    f.close()

        if view is not MISSING:
            if ephemeral and view.timeout is None:
                view.timeout = 15 * 60.0

            self._parent._state.store_view(view)

        if delete_after is not MISSING:
            await self._parent.delete_original_response(delay=delete_after)

    async def edit_message(
        self,
        content: Optional[str] = MISSING,
        *,
        embed: Optional[Embed] = MISSING,
        embeds: List[Embed] = MISSING,
        file: File = MISSING,
        files: List[File] = MISSING,
        attachments: Optional[List[Attachment]] = MISSING,
        allowed_mentions: AllowedMentions = MISSING,
        view: Optional[View] = MISSING,
        components: Optional[Components[MessageUIComponent]] = MISSING,
    ) -> None:
        """|coro|

        Responds to this interaction by editing the original message of
        a component interaction or modal interaction (if the modal was sent in
        response to a component interaction).

        .. versionchanged:: 2.5

            Now supports editing the original message of modal interactions that started from a component.

        .. note::
            If the original message has embeds with images that were created from local files
            (using the ``file`` parameter with :meth:`Embed.set_image` or :meth:`Embed.set_thumbnail`),
            those images will be removed if the message's attachments are edited in any way
            (i.e. by setting ``file``/``files``/``attachments``, or adding an embed with local files).

        Parameters
        ----------
        content: Optional[:class:`str`]
            The new content to replace the message with. ``None`` removes the content.
        embed: Optional[:class:`Embed`]
            The new embed to replace the original with. This cannot be mixed with the
            ``embeds`` parameter.
            Could be ``None`` to remove the embed.
        embeds: List[:class:`Embed`]
            The new embeds to replace the original with. Must be a maximum of 10.
            This cannot be mixed with the ``embed`` parameter.
            To remove all embeds ``[]`` should be passed.
        file: :class:`File`
            The file to upload. This cannot be mixed with the ``files`` parameter.
            Files will be appended to the message.

            .. versionadded:: 2.2

        files: List[:class:`File`]
            A list of files to upload. This cannot be mixed with the ``file`` parameter.
            Files will be appended to the message.

            .. versionadded:: 2.2

        attachments: Optional[List[:class:`Attachment`]]
            A list of attachments to keep in the message.
            If ``[]`` or ``None`` is passed then all existing attachments are removed.
            Keeps existing attachments if not provided.

            .. versionadded:: 2.4

            .. versionchanged:: 2.5
                Supports passing ``None`` to clear attachments.

        allowed_mentions: :class:`AllowedMentions`
            Controls the mentions being processed in this message.
        view: Optional[:class:`~disnake.ui.View`]
            The updated view to update this message with. This cannot be mixed with ``components``.
            If ``None`` is passed then the view is removed.
        components: Optional[|components_type|]
            A list of components to update this message with. This cannot be mixed with ``view``.
            If ``None`` is passed then the components are removed.

            .. versionadded:: 2.4

        Raises
        ------
        HTTPException
            Editing the message failed.
        TypeError
            You specified both ``embed`` and ``embeds``.
        InteractionResponded
            This interaction has already been responded to before.
        """
        if self._response_type is not None:
            raise InteractionResponded(self._parent)

        parent = self._parent
        state = parent._state

        if parent.type not in (InteractionType.component, InteractionType.modal_submit):
            raise InteractionNotEditable(parent)
        parent = cast("Union[MessageInteraction, ModalInteraction]", parent)
        message = parent.message
        # message in modal interactions only exists if modal was sent from component interaction
        if not message:
            raise InteractionNotEditable(parent)

        payload = {}
        if content is not MISSING:
            payload["content"] = None if content is None else str(content)

        if file is not MISSING and files is not MISSING:
            raise TypeError("cannot mix file and files keyword arguments")

        if file is not MISSING:
            files = [file]

        if embed is not MISSING and embeds is not MISSING:
            raise TypeError("cannot mix both embed and embeds keyword arguments")

        if embed is not MISSING:
            embeds = [] if embed is None else [embed]
        if embeds is not MISSING:
            payload["embeds"] = [e.to_dict() for e in embeds]
            for embed in embeds:
                if embed._files:
                    files = files or []
                    files.extend(embed._files.values())

        if files is not MISSING and len(files) > 10:
            raise ValueError("files cannot exceed maximum of 10 elements")

        previous_mentions: Optional[AllowedMentions] = getattr(
            self._parent._state, "allowed_mentions", None
        )
        if allowed_mentions:
            if previous_mentions is not None:
                payload["allowed_mentions"] = previous_mentions.merge(allowed_mentions).to_dict()
            else:
                payload["allowed_mentions"] = allowed_mentions.to_dict()
        elif previous_mentions is not None:
            payload["allowed_mentions"] = previous_mentions.to_dict()

        # if no attachment list was provided but we're uploading new files,
        # use current attachments as the base
        if attachments is MISSING and (file or files):
            attachments = message.attachments
        if attachments is not MISSING:
            payload["attachments"] = (
                [] if attachments is None else [a.to_dict() for a in attachments]
            )

        if view is not MISSING and components is not MISSING:
            raise TypeError("cannot mix view and components keyword arguments")

        if view is not MISSING:
            state.prevent_view_updates_for(message.id)
            payload["components"] = [] if view is None else view.to_components()

        if components is not MISSING:
            payload["components"] = [] if components is None else components_to_dict(components)

        adapter = async_context.get()
        response_type = InteractionResponseType.message_update
        try:
            async with parent._response_lock:
                await adapter.create_interaction_response(
                    parent.id,
                    parent.token,
                    session=parent._session,
                    type=response_type.value,
                    data=payload,
                    files=files,
                )
                self._response_type = response_type
        finally:
            if files:
                for f in files:
                    f.close()

        if view and not view.is_finished():
            state.store_view(view, message.id)

    async def autocomplete(self, *, choices: Choices) -> None:
        """|coro|

        Responds to this interaction by displaying a list of possible autocomplete results.
        Only works for autocomplete interactions.

        Parameters
        ----------
        choices: Union[List[:class:`OptionChoice`], List[Union[:class:`str`, :class:`int`]], Dict[:class:`str`, Union[:class:`str`, :class:`int`]]]
            The list of choices to suggest.

        Raises
        ------
        HTTPException
            Autocomplete response has failed.
        InteractionResponded
            This interaction has already been responded to before.
        """
        if self._response_type is not None:
            raise InteractionResponded(self._parent)

        choices_data: List[ApplicationCommandOptionChoicePayload]
        if isinstance(choices, Mapping):
            choices_data = [{"name": n, "value": v} for n, v in choices.items()]
        else:
            choices_data = []
            value: ApplicationCommandOptionChoicePayload
            i18n = self._parent.client.i18n
            for c in choices:
                if isinstance(c, Localized):
                    c = OptionChoice(c, c.string)

                if isinstance(c, OptionChoice):
                    c.localize(i18n)
                    value = c.to_dict(locale=self._parent.locale)
                else:
                    value = {"name": str(c), "value": c}
                choices_data.append(value)

        parent = self._parent
        adapter = async_context.get()
        response_type = InteractionResponseType.application_command_autocomplete_result
        async with parent._response_lock:
            await adapter.create_interaction_response(
                parent.id,
                parent.token,
                session=parent._session,
                type=response_type.value,
                data={"choices": choices_data},
            )
            self._response_type = response_type

    @overload
    async def send_modal(self, modal: Modal) -> None:
        ...

    @overload
    async def send_modal(
        self,
        *,
        title: str,
        custom_id: str,
        components: Components[ModalUIComponent],
    ) -> None:
        ...

    async def send_modal(
        self,
        modal: Optional[Modal] = None,
        *,
        title: Optional[str] = None,
        custom_id: Optional[str] = None,
        components: Optional[Components[ModalUIComponent]] = None,
    ) -> None:
        """|coro|

        Responds to this interaction by displaying a modal.

        .. versionadded:: 2.4

        .. note::

            Not passing the ``modal`` parameter here will not register a callback, and a :func:`on_modal_submit`
            interaction will need to be handled manually.

        Parameters
        ----------
        modal: :class:`~.ui.Modal`
            The modal to display. This cannot be mixed with the ``title``, ``custom_id`` and ``components`` parameters.
        title: :class:`str`
            The title of the modal. This cannot be mixed with the ``modal`` parameter.
        custom_id: :class:`str`
            The ID of the modal that gets received during an interaction.
            This cannot be mixed with the ``modal`` parameter.
        components: |components_type|
            The components to display in the modal. A maximum of 5.
            This cannot be mixed with the ``modal`` parameter.

        Raises
        ------
        TypeError
            Cannot mix the ``modal`` parameter and the ``title``, ``custom_id``, ``components`` parameters.
        ValueError
            Maximum number of components (5) exceeded.
        HTTPException
            Displaying the modal failed.
        ModalChainNotSupported
            This interaction cannot be responded with a modal.
        InteractionResponded
            This interaction has already been responded to before.
        """
        if modal is not None and any((title, components, custom_id)):
            raise TypeError("Cannot mix modal argument and title, custom_id, components arguments")

        parent = self._parent

        if parent.type is InteractionType.modal_submit:
            raise ModalChainNotSupported(parent)  # type: ignore

        if self._response_type is not None:
            raise InteractionResponded(parent)

        modal_data: ModalPayload

        if modal is not None:
            modal_data = modal.to_components()
        elif title and components and custom_id:

            rows = components_to_dict(components)
            if len(rows) > 5:
                raise ValueError("Maximum number of components exceeded.")

            modal_data = {
                "title": title,
                "custom_id": custom_id,
                "components": rows,
            }
        else:
            raise TypeError("Either modal or title, custom_id, components must be provided")

        adapter = async_context.get()
        response_type = InteractionResponseType.modal
        async with parent._response_lock:
            await adapter.create_interaction_response(
                parent.id,
                parent.token,
                session=parent._session,
                type=response_type.value,
                data=modal_data,  # type: ignore
            )
            self._response_type = response_type

        if modal is not None:
            parent._state.store_modal(parent.author.id, modal)


class _InteractionMessageState:
    __slots__ = ("_parent", "_interaction")

    def __init__(self, interaction: Interaction, parent: ConnectionState):
        self._interaction: Interaction = interaction
        self._parent: ConnectionState = parent

    def _get_guild(self, guild_id):
        return self._parent._get_guild(guild_id)

    def store_user(self, data):
        return self._parent.store_user(data)

    def create_user(self, data):
        return self._parent.create_user(data)

    @property
    def http(self):
        return self._parent.http

    def __getattr__(self, attr):
        return getattr(self._parent, attr)


class InteractionMessage(Message):
    """Represents the original interaction response message.

    This allows you to edit or delete the message associated with
    the interaction response. To retrieve this object see :meth:`Interaction.original_response`.

    This inherits from :class:`disnake.Message` with changes to
    :meth:`edit` and :meth:`delete` to work.

    .. versionadded:: 2.0

    Attributes
    ----------
    type: :class:`MessageType`
        The type of message.
    author: Union[:class:`Member`, :class:`abc.User`]
        A :class:`Member` that sent the message. If :attr:`channel` is a
        private channel, then it is a :class:`User` instead.
    content: :class:`str`
        The actual contents of the message.
    embeds: List[:class:`Embed`]
        A list of embeds the message has.
    channel: Union[:class:`TextChannel`, :class:`VoiceChannel`, :class:`Thread`, :class:`DMChannel`, :class:`GroupChannel`, :class:`PartialMessageable`]
        The channel that the message was sent from.
        Could be a :class:`DMChannel` or :class:`GroupChannel` if it's a private message.
    reference: Optional[:class:`~disnake.MessageReference`]
        The message that this message references. This is only applicable to message replies.
    interaction: Optional[:class:`~disnake.InteractionReference`]
        The interaction that this message references.
        This exists only when the message is a response to an interaction without an existing message.

        .. versionadded:: 2.1

    mention_everyone: :class:`bool`
        Specifies if the message mentions everyone.

        .. note::

            This does not check if the ``@everyone`` or the ``@here`` text is in the message itself.
            Rather this boolean indicates if either the ``@everyone`` or the ``@here`` text is in the message
            **and** it did end up mentioning.
    mentions: List[:class:`abc.User`]
        A list of :class:`Member` that were mentioned. If the message is in a private message
        then the list will be of :class:`User` instead.

        .. warning::

            The order of the mentions list is not in any particular order so you should
            not rely on it. This is a Discord limitation, not one with the library.
    role_mentions: List[:class:`Role`]
        A list of :class:`Role` that were mentioned. If the message is in a private message
        then the list is always empty.
    id: :class:`int`
        The message ID.
    webhook_id: Optional[:class:`int`]
        The ID of the application that sent this message.
    attachments: List[:class:`Attachment`]
        A list of attachments given to a message.
    pinned: :class:`bool`
        Specifies if the message is currently pinned.
    flags: :class:`MessageFlags`
        Extra features of the message.
    reactions : List[:class:`Reaction`]
        Reactions to a message. Reactions can be either custom emoji or standard unicode emoji.
    stickers: List[:class:`StickerItem`]
        A list of sticker items given to the message.
    components: List[:class:`Component`]
        A list of components in the message.
    guild: Optional[:class:`Guild`]
        The guild that the message belongs to, if applicable.
    """

    __slots__ = ()
    _state: _InteractionMessageState

    @overload
    async def edit(
        self,
        content: Optional[str] = ...,
        *,
        embed: Optional[Embed] = ...,
        file: File = ...,
        attachments: Optional[List[Attachment]] = ...,
        allowed_mentions: Optional[AllowedMentions] = ...,
        view: Optional[View] = ...,
        components: Optional[Components[MessageUIComponent]] = ...,
    ) -> InteractionMessage:
        ...

    @overload
    async def edit(
        self,
        content: Optional[str] = ...,
        *,
        embed: Optional[Embed] = ...,
        files: List[File] = ...,
        attachments: Optional[List[Attachment]] = ...,
        allowed_mentions: Optional[AllowedMentions] = ...,
        view: Optional[View] = ...,
        components: Optional[Components[MessageUIComponent]] = ...,
    ) -> InteractionMessage:
        ...

    @overload
    async def edit(
        self,
        content: Optional[str] = ...,
        *,
        embeds: List[Embed] = ...,
        file: File = ...,
        attachments: Optional[List[Attachment]] = ...,
        allowed_mentions: Optional[AllowedMentions] = ...,
        view: Optional[View] = ...,
        components: Optional[Components[MessageUIComponent]] = ...,
    ) -> InteractionMessage:
        ...

    @overload
    async def edit(
        self,
        content: Optional[str] = ...,
        *,
        embeds: List[Embed] = ...,
        files: List[File] = ...,
        attachments: Optional[List[Attachment]] = ...,
        allowed_mentions: Optional[AllowedMentions] = ...,
        view: Optional[View] = ...,
        components: Optional[Components[MessageUIComponent]] = ...,
    ) -> InteractionMessage:
        ...

    async def edit(
        self,
        content: Optional[str] = MISSING,
        *,
        embed: Optional[Embed] = MISSING,
        embeds: List[Embed] = MISSING,
        file: File = MISSING,
        files: List[File] = MISSING,
        attachments: Optional[List[Attachment]] = MISSING,
        allowed_mentions: Optional[AllowedMentions] = MISSING,
        view: Optional[View] = MISSING,
        components: Optional[Components[MessageUIComponent]] = MISSING,
    ) -> Message:
        """|coro|

        Edits the message.

        .. note::
            If the original message has embeds with images that were created from local files
            (using the ``file`` parameter with :meth:`Embed.set_image` or :meth:`Embed.set_thumbnail`),
            those images will be removed if the message's attachments are edited in any way
            (i.e. by setting ``file``/``files``/``attachments``, or adding an embed with local files).

        Parameters
        ----------
        content: Optional[:class:`str`]
            The content to edit the message with, or ``None`` to clear it.
        embed: Optional[:class:`Embed`]
            The new embed to replace the original with. This cannot be mixed with the
            ``embeds`` parameter.
            Could be ``None`` to remove the embed.
        embeds: List[:class:`Embed`]
            The new embeds to replace the original with. Must be a maximum of 10.
            This cannot be mixed with the ``embed`` parameter.
            To remove all embeds ``[]`` should be passed.
        file: :class:`File`
            The file to upload. This cannot be mixed with the ``files`` parameter.
            Files will be appended to the message, see the ``attachments`` parameter
            to remove/replace existing files.
        files: List[:class:`File`]
            A list of files to upload. This cannot be mixed with the ``file`` parameter.
            Files will be appended to the message, see the ``attachments`` parameter
            to remove/replace existing files.
        attachments: Optional[List[:class:`Attachment`]]
            A list of attachments to keep in the message.
            If ``[]`` or ``None`` is passed then all existing attachments are removed.
            Keeps existing attachments if not provided.

            .. versionadded:: 2.2

            .. versionchanged:: 2.5
                Supports passing ``None`` to clear attachments.

        view: Optional[:class:`~disnake.ui.View`]
            The updated view to update this message with. This cannot be mixed with ``components``.
            If ``None`` is passed then the view is removed.
        components: Optional[|components_type|]
            A list of components to update this message with. This cannot be mixed with ``view``.
            If ``None`` is passed then the components are removed.

            .. versionadded:: 2.4

        allowed_mentions: :class:`AllowedMentions`
            Controls the mentions being processed in this message.
            See :meth:`.abc.Messageable.send` for more information.

        Raises
        ------
        HTTPException
            Editing the message failed.
        Forbidden
            Edited a message that is not yours.
        TypeError
            You specified both ``embed`` and ``embeds`` or ``file`` and ``files``
        ValueError
            The length of ``embeds`` was invalid.

        Returns
        -------
        :class:`InteractionMessage`
            The newly edited message.
        """
        if self._state._interaction.is_expired():
            # We have to choose between type-ignoring the entire call,
            # or not having these specific parameters type-checked,
            # as we'd otherwise not match any of the overloads if all
            # parameters were provided
            params = {"file": file, "embed": embed}
            return await super().edit(
                content=content,
                embeds=embeds,
                files=files,
                attachments=attachments,
                allowed_mentions=allowed_mentions,
                view=view,
                components=components,
                **params,
            )

        # if no attachment list was provided but we're uploading new files,
        # use current attachments as the base
        # this isn't necessary when using the superclass, as the implementation there takes care of attachments
        if attachments is MISSING and (file or files):
            attachments = self.attachments

        return await self._state._interaction.edit_original_response(
            content=content,
            embed=embed,
            embeds=embeds,
            file=file,
            files=files,
            attachments=attachments,
            allowed_mentions=allowed_mentions,
            view=view,
            components=components,
        )

    async def delete(self, *, delay: Optional[float] = None) -> None:
        """|coro|

        Deletes the message.

        Parameters
        ----------
        delay: Optional[:class:`float`]
            If provided, the number of seconds to wait before deleting the message.
            The waiting is done in the background and deletion failures are ignored.

        Raises
        ------
        Forbidden
            You do not have proper permissions to delete the message.
        NotFound
            The message was deleted already.
        HTTPException
            Deleting the message failed.
        """
        if self._state._interaction.is_expired():
            return await super().delete(delay=delay)
        if delay is not None:

            async def inner_call(delay: float = delay):
                await asyncio.sleep(delay)
                try:
                    await self._state._interaction.delete_original_response()
                except HTTPException:
                    pass

            asyncio.create_task(inner_call())
        else:
            await self._state._interaction.delete_original_response()
