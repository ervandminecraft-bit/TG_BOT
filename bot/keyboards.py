"""Callback data factories and common keyboards."""
from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.i18n import t


class MenuCB(CallbackData, prefix="m"):
    action: str  # main | catalog | mysub | promo | ref | support | lang | admin | relink


class LangCB(CallbackData, prefix="l"):
    code: str


class PlanCB(CallbackData, prefix="p"):
    action: str  # view | buy
    plan_id: int
    method_id: int = 0


class OrderCB(CallbackData, prefix="o"):
    action: str  # paid | cancel
    order_id: int


class ReviewCB(CallbackData, prefix="r"):
    """Admin actions on an order card."""

    action: str  # ok | no | tpl | custom | back
    order_id: int
    extra: str = ""


class AdminCB(CallbackData, prefix="a"):
    """Generic admin panel navigation: section / action / id / extra."""

    s: str
    a: str = ""
    i: int = 0
    x: str = ""


def back_to_menu_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=t(lang, "btn_main_menu"), callback_data=MenuCB(action="main").pack())
    ]])


def main_menu_kb(lang: str, is_admin: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=t(lang, "btn_catalog"), callback_data=MenuCB(action="catalog"))
    b.button(text=t(lang, "btn_mysub"), callback_data=MenuCB(action="mysub"))
    b.button(text=t(lang, "btn_promo"), callback_data=MenuCB(action="promo"))
    b.button(text=t(lang, "btn_ref"), callback_data=MenuCB(action="ref"))
    b.button(text=t(lang, "btn_support"), callback_data=MenuCB(action="support"))
    b.button(text=t(lang, "btn_lang"), callback_data=MenuCB(action="lang"))
    if is_admin:
        b.button(text=t(lang, "btn_admin"), callback_data=AdminCB(s="home"))
    b.adjust(1, 1, 2, 2, 1)
    return b.as_markup()


def renew_kb(lang: str, plan_id: int | None) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if plan_id:
        b.button(text=t(lang, "btn_renew"), callback_data=PlanCB(action="view", plan_id=plan_id))
    b.button(text=t(lang, "btn_catalog"), callback_data=MenuCB(action="catalog"))
    b.adjust(1)
    return b.as_markup()


def admin_back_kb(lang: str, section: str = "home", action: str = "", item_id: int = 0) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=t(lang, "btn_back"),
                             callback_data=AdminCB(s=section, a=action, i=item_id).pack())
    ]])
