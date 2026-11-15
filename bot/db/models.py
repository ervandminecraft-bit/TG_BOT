"""Database models. All datetimes are stored as naive UTC."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from bot.utils.timeutil import utcnow


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------- statuses
class OrderStatus:
    AWAITING_PAYMENT = "awaiting_payment"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    EXPIRED = "expired"

    ALL = (AWAITING_PAYMENT, PENDING_REVIEW, APPROVED, REJECTED, CANCELLED, EXPIRED)
    OPEN = (AWAITING_PAYMENT, PENDING_REVIEW, REJECTED)


class SubStatus:
    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"


plan_channels = Table(
    "plan_channels",
    Base.metadata,
    Column("plan_id", ForeignKey("plans.id", ondelete="CASCADE"), primary_key=True),
    Column("channel_id", ForeignKey("channels.id", ondelete="CASCADE"), primary_key=True),
)

promo_plans = Table(
    "promo_plans",
    Base.metadata,
    Column("promo_id", ForeignKey("promo_codes.id", ondelete="CASCADE"), primary_key=True),
    Column("plan_id", ForeignKey("plans.id", ondelete="CASCADE"), primary_key=True),
)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # Telegram user id
    username: Mapped[str | None] = mapped_column(String(64), index=True)
    first_name: Mapped[str | None] = mapped_column(String(128))
    last_name: Mapped[str | None] = mapped_column(String(128))
    language: Mapped[str] = mapped_column(String(5), default="ru")
    ref_code: Mapped[str] = mapped_column(String(16), unique=True)
    referred_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    active_promo_id: Mapped[int | None] = mapped_column(ForeignKey("promo_codes.id", ondelete="SET NULL"))
    bonus_days_balance: Mapped[int] = mapped_column(Integer, default=0)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)  # user blocked the bot
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PaymentMethodInfo(Base):
    __tablename__ = "payment_methods"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    details: Mapped[str] = mapped_column(Text, default="")
    currency: Mapped[str] = mapped_column(String(10))
    provider: Mapped[str] = mapped_column(String(32), default="manual")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")
    duration_days: Mapped[int | None] = mapped_column(Integer)  # None = lifetime
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    channels: Mapped[list[Channel]] = relationship(secondary=plan_channels, lazy="selectin", order_by=Channel.id)
    prices: Mapped[list["PlanPrice"]] = relationship(
        back_populates="plan", lazy="selectin", cascade="all, delete-orphan"
    )

    @property
    def is_lifetime(self) -> bool:
        return self.duration_days is None


class PlanPrice(Base):
    __tablename__ = "plan_prices"
    __table_args__ = (UniqueConstraint("plan_id", "payment_method_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id", ondelete="CASCADE"))
    payment_method_id: Mapped[int] = mapped_column(ForeignKey("payment_methods.id", ondelete="CASCADE"))
    amount: Mapped[int] = mapped_column(Integer)  # minor units (cents/kopecks)

    plan: Mapped[Plan] = relationship(back_populates="prices")


class PromoCode(Base):
    __tablename__ = "promo_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    kind: Mapped[str] = mapped_column(String(10), default="percent")  # percent | fixed | none
    value: Mapped[int] = mapped_column(Integer, default=0)  # percent, or minor units for fixed
    currency: Mapped[str | None] = mapped_column(String(10))  # for fixed discounts
    bonus_days: Mapped[int] = mapped_column(Integer, default=0)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime)
    max_uses: Mapped[int | None] = mapped_column(Integer)  # None = unlimited
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    once_per_user: Mapped[bool] = mapped_column(Boolean, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    plans: Mapped[list[Plan]] = relationship(secondary=promo_plans, lazy="selectin")


class PromoUsage(Base):
    __tablename__ = "promo_usages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    promo_id: Mapped[int] = mapped_column(ForeignKey("promo_codes.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(12), unique=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"))
    payment_method_id: Mapped[int] = mapped_column(ForeignKey("payment_methods.id"))
    provider: Mapped[str] = mapped_column(String(32), default="manual")
    currency: Mapped[str] = mapped_column(String(10))
    base_amount: Mapped[int] = mapped_column(Integer)
    discount_amount: Mapped[int] = mapped_column(Integer, default=0)
    final_amount: Mapped[int] = mapped_column(Integer)
    promo_id: Mapped[int | None] = mapped_column(ForeignKey("promo_codes.id", ondelete="SET NULL"))
    referral_discount: Mapped[bool] = mapped_column(Boolean, default=False)
    bonus_days: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default=OrderStatus.AWAITING_PAYMENT, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)

    proof_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    proof_submitted_at: Mapped[datetime | None] = mapped_column(DateTime)
    proof_type: Mapped[str | None] = mapped_column(String(10))  # photo | document | text
    proof_file_id: Mapped[str | None] = mapped_column(String(255))
    proof_file_unique_id: Mapped[str | None] = mapped_column(String(64), index=True)
    proof_text: Mapped[str | None] = mapped_column(Text)
    proof_text_norm: Mapped[str | None] = mapped_column(String(255), index=True)

    reviewed_by: Mapped[int | None] = mapped_column(BigInteger)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    reject_reason: Mapped[str | None] = mapped_column(Text)

    access_granted: Mapped[bool] = mapped_column(Boolean, default=False)
    access_attempts: Mapped[int] = mapped_column(Integer, default=0)
    access_last_error: Mapped[str | None] = mapped_column(Text)
    refunded: Mapped[bool] = mapped_column(Boolean, default=False)


class OrderAdminMessage(Base):
    """Where each admin received the order card (to update it after a decision)."""

    __tablename__ = "order_admin_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(BigInteger)


class Subscription(Base):
    __tablename__ = "subscriptions"
    __table_args__ = (UniqueConstraint("user_id", "plan_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"))
    status: Mapped[str] = mapped_column(String(10), default=SubStatus.ACTIVE, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)  # None = lifetime
    # The reminder flags hold the expiry value they were sent for, so a renewal
    # (new expiry) re-enables reminders automatically and duplicates are impossible.
    reminded_first_for: Mapped[datetime | None] = mapped_column(DateTime)
    reminded_second_for: Mapped[datetime | None] = mapped_column(DateTime)
    expired_notified_for: Mapped[datetime | None] = mapped_column(DateTime)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime)
    source: Mapped[str] = mapped_column(String(20), default="order")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class InviteLink(Base):
    __tablename__ = "invite_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"))
    link: Mapped[str] = mapped_column(String(255), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    used_at: Mapped[datetime | None] = mapped_column(DateTime)


class ChannelMember(Base):
    """Membership as seen by the bot (join requests and chat_member updates)."""

    __tablename__ = "channel_members"
    __table_args__ = (UniqueConstraint("user_id", "channel_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(20))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ReferralReward(Base):
    __tablename__ = "referral_rewards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    referrer_id: Mapped[int] = mapped_column(BigInteger, index=True)
    referred_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"), nullable=True)
    bonus_days: Mapped[int] = mapped_column(Integer, default=0)
    applied_to: Mapped[str] = mapped_column(String(20), default="subscription")  # subscription | balance
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    admin_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    action: Mapped[str] = mapped_column(String(40))
    target: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Broadcast(Base):
    __tablename__ = "broadcasts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    admin_id: Mapped[int] = mapped_column(BigInteger)
    segment: Mapped[str] = mapped_column(String(16))
    from_chat_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running | done | interrupted
    total: Mapped[int] = mapped_column(Integer, default=0)
    sent: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class KV(Base):
    """Small key-value store for job state (last runs, heartbeat...)."""

    __tablename__ = "kv"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class FSMRecord(Base):
    """Persistent FSM state for dialogs (survives restarts)."""

    __tablename__ = "fsm_states"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    state: Mapped[str | None] = mapped_column(String(128))
    data: Mapped[str] = mapped_column(Text, default="{}")
