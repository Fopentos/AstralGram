#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import asyncio
import hashlib
import json
import random
import string
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Union

import apscheduler
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean, Text, ForeignKey, BigInteger, Float
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, BotCommand
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    ContextTypes, CallbackQueryHandler, ConversationHandler
)
from telegram.error import BadRequest, TelegramError

# ----------------------------- КОНФИГУРАЦИЯ -----------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not set")

ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(","))) if os.getenv("ADMIN_IDS") else []
SUPPORT_LINK = os.getenv("SUPPORT_LINK", "https://t.me/your_support")
RULES_LINK = os.getenv("RULES_LINK", "https://telegra.ph/Rules-05-09")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///promo_bot.db")

CURRENCY = "💷"
REFERRAL_BONUS = 100               # бонус за приглашённого
REFERRAL_PERCENT = 10               # % от заработка реферала (если уровень)
DAILY_BONUS_AMOUNT = 50
XP_PER_TASK = 20
XP_LEVEL_BASE = 500                 # XP для 2 уровня, потом 1000, 1500...
MAX_TASKS_PER_PAGE = 10
UNSUBSCRIBE_BAN_DAYS = 7
AUTO_APPROVE_HOURS = 24
TASK_DURATION_DAYS = 7
MAX_ACTIVE_TASKS = 10
MAX_ACTIVE_TASKS_PER_USER = 5
COMMISSION_RATE = 0.10              # 10% комиссия при оплате заработанными монетами

# ----------------------------- БАЗА ДАННЫХ -----------------------------
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, unique=True, nullable=False)
    username = Column(String)
    first_name = Column(String)
    balance = Column(Integer, default=0)            # общий баланс
    earned_balance = Column(Integer, default=0)     # заработанные монеты (для комиссии)
    referral_code = Column(String, unique=True)
    referred_by = Column(BigInteger, nullable=True)
    level = Column(Integer, default=1)
    xp = Column(Integer, default=0)
    daily_streak = Column(Integer, default=0)
    last_daily = Column(DateTime, nullable=True)
    join_date = Column(DateTime, default=datetime.now)
    is_banned = Column(Boolean, default=False)
    ban_until = Column(DateTime, nullable=True)
    lang = Column(String, default="ru")

    # связи
    channels = relationship("Channel", back_populates="owner")
    subscriptions = relationship("Subscription", back_populates="user")
    tasks = relationship("Task", back_populates="creator")
    complaints = relationship("Complaint", foreign_keys="Complaint.from_user_id")

class Channel(Base):
    __tablename__ = "channels"
    id = Column(Integer, primary_key=True)
    channel_id = Column(String, unique=True, nullable=False)
    channel_name = Column(String)
    owner_id = Column(BigInteger, ForeignKey("users.user_id"))
    invite_link = Column(String)
    is_verified = Column(Boolean, default=False)
    added_date = Column(DateTime, default=datetime.now)
    owner = relationship("User", back_populates="channels")
    tasks = relationship("Task", back_populates="channel")

class Task(Base):
    __tablename__ = "tasks"
    id = Column(Integer, primary_key=True)
    type = Column(String, nullable=False)  # channel, group, post, bot, boost, reaction
    target_id = Column(String)             # channel_id, group_id, bot_username, post_link
    target_name = Column(String)
    reward = Column(Integer, nullable=False)
    max_completions = Column(Integer, default=50)
    current_completions = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    is_paused = Column(Boolean, default=False)
    creator_id = Column(BigInteger, ForeignKey("users.user_id"))
    created_at = Column(DateTime, default=datetime.now)
    expires_at = Column(DateTime, nullable=False)
    extra_data = Column(Text, nullable=True)   # для реакции: тип реакции, для поста: ссылка и т.д.

    channel_id = Column(String, ForeignKey("channels.channel_id"), nullable=True)
    channel = relationship("Channel", back_populates="tasks")
    creator = relationship("User", back_populates="tasks")
    completions = relationship("TaskCompletion", back_populates="task")

class TaskCompletion(Base):
    __tablename__ = "task_completions"
    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey("tasks.id"))
    user_id = Column(BigInteger, ForeignKey("users.user_id"))
    completed_at = Column(DateTime, default=datetime.now)
    is_verified = Column(Boolean, default=False)
    screenshot_message_id = Column(Integer, nullable=True)   # для реакций/просмотров
    approved_at = Column(DateTime, nullable=True)
    user = relationship("User", back_populates="subscriptions")
    task = relationship("Task", back_populates="completions")

class Subscription(Base):
    __tablename__ = "subscriptions"
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, ForeignKey("users.user_id"))
    channel_id = Column(String, nullable=False)   # для какого канала подписка (чтобы отслеживать отписки)
    task_id = Column(Integer, ForeignKey("tasks.id"))
    subscribed_at = Column(DateTime, default=datetime.now)
    check_until = Column(DateTime, nullable=False)  # до какой даты нельзя отписываться
    user = relationship("User", back_populates="subscriptions")

class Transaction(Base):
    __tablename__ = "transactions"
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, ForeignKey("users.user_id"))
    amount = Column(Integer)
    type = Column(String)   # deposit, task_creation, task_reward, referral, daily, refund
    description = Column(Text)
    date = Column(DateTime, default=datetime.now)

class Complaint(Base):
    __tablename__ = "complaints"
    id = Column(Integer, primary_key=True)
    from_user_id = Column(BigInteger, ForeignKey("users.user_id"))
    about_task_id = Column(Integer, ForeignKey("tasks.id"), nullable=True)
    about_user_id = Column(BigInteger, nullable=True)
    reason = Column(Text)
    status = Column(String, default="new")  # new, reviewed, rejected
    created_at = Column(DateTime, default=datetime.now)

class Blacklist(Base):
    __tablename__ = "blacklist"
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, unique=True)
    reason = Column(Text)
    banned_by = Column(BigInteger)
    banned_at = Column(DateTime, default=datetime.now)

class ReferralLink(Base):
    __tablename__ = "referral_links"
    id = Column(Integer, primary_key=True)
    code = Column(String, unique=True)
    owner_id = Column(BigInteger, ForeignKey("users.user_id"))
    created_at = Column(DateTime, default=datetime.now)
    clicks = Column(Integer, default=0)

class Check(Base):
    __tablename__ = "checks"
    id = Column(Integer, primary_key=True)
    type = Column(String)   # personal, multi
    owner_id = Column(BigInteger, ForeignKey("users.user_id"))
    amount = Column(Integer)
    total_amount = Column(Integer)   # для мульти: общий бюджет
    remaining = Column(Integer)
    max_uses = Column(Integer, default=1)
    used_count = Column(Integer, default=0)
    required_channel = Column(String, nullable=True)   # условие подписки для мульти-чека
    code = Column(String, unique=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)
    expires_at = Column(DateTime, nullable=True)

# Создание движка
engine = create_engine(DATABASE_URL)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)

# ----------------------------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ -----------------------------
def get_user(user_id: int) -> Optional[User]:
    session = Session()
    user = session.query(User).filter_by(user_id=user_id).first()
    if not user:
        # создаём нового
        referral_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
        user = User(user_id=user_id, referral_code=referral_code, balance=0, earned_balance=0)
        session.add(user)
        session.commit()
    session.close()
    return user

def add_transaction(user_id: int, amount: int, trans_type: str, description: str):
    session = Session()
    trans = Transaction(user_id=user_id, amount=amount, type=trans_type, description=description)
    session.add(trans)
    session.commit()
    session.close()

def update_user_balance(user_id: int, delta: int, is_earned: bool = False):
    session = Session()
    user = session.query(User).filter_by(user_id=user_id).first()
    if user:
        user.balance += delta
        if is_earned and delta > 0:
            user.earned_balance += delta
        session.commit()
    session.close()

def add_xp(user_id: int, xp: int):
    session = Session()
    user = session.query(User).filter_by(user_id=user_id).first()
    if user:
        user.xp += xp
        # проверка повышения уровня
        next_level_xp = user.level * XP_LEVEL_BASE
        while user.xp >= next_level_xp:
            user.level += 1
            user.xp -= next_level_xp
            next_level_xp = user.level * XP_LEVEL_BASE
            # бонус за уровень
            level_bonus = user.level * 50
            user.balance += level_bonus
            add_transaction(user_id, level_bonus, "level_up", f"Бонус за {user.level} уровень")
        session.commit()
    session.close()

# ----------------------------- КЛАВИАТУРЫ -----------------------------
def main_keyboard():
    keyboard = [
        [InlineKeyboardButton("💰 Заработать", callback_data="earn")],
        [InlineKeyboardButton("📢 Рекламировать", callback_data="advertise")],
        [InlineKeyboardButton("💸 Чеки", callback_data="checks_menu")],
        [InlineKeyboardButton("👤 Мой кабинет", callback_data="cabinet")]
    ]
    return InlineKeyboardMarkup(keyboard)

def earn_type_keyboard():
    keyboard = [
        [InlineKeyboardButton("📺 Каналы", callback_data="earn_channels")],
        [InlineKeyboardButton("👥 Группы", callback_data="earn_groups")],
        [InlineKeyboardButton("👁 Просмотры", callback_data="earn_views")],
        [InlineKeyboardButton("🔥 Реакции", callback_data="earn_reactions")],
        [InlineKeyboardButton("🤖 Боты", callback_data="earn_bots")],
        [InlineKeyboardButton("⚡ Премиум буст", callback_data="earn_boost")],
        [InlineKeyboardButton("🔙 Назад", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

def back_keyboard(callback: str = "main_menu"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data=callback)]])

def pagination_keyboard(page: int, total_pages: int, base_callback: str, extra_buttons: list = None):
    buttons = []
    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️", callback_data=f"{base_callback}_prev"))
        nav.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="ignore"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("▶️", callback_data=f"{base_callback}_next"))
        buttons.append(nav)
    if extra_buttons:
        buttons.extend(extra_buttons)
    buttons.append([InlineKeyboardButton("🔙 Назад", callback_data="earn")])
    return InlineKeyboardMarkup(buttons)

def task_item_keyboard(task: Task, page: int, idx: int):
    # для каждого задания: ссылка и кнопка проверки
    if task.type in ("channel", "group", "bot"):
        text = f"🔗 Перейти: {task.target_name} | {task.reward}{CURRENCY}"
        url = task.extra_data if task.extra_data else f"https://t.me/{task.target_name}"
        check_cb = f"check_task_{task.id}"
        return [
            InlineKeyboardButton(text, url=url),
            InlineKeyboardButton("✅ Проверить", callback_data=check_cb)
        ]
    elif task.type == "post":
        text = f"👁 Просмотр: {task.target_name} | {task.reward}{CURRENCY}"
        return [InlineKeyboardButton(text, callback_data=f"view_task_{task.id}")]
    elif task.type == "reaction":
        text = f"🔥 Реакция: {task.target_name} | {task.reward}{CURRENCY}"
        return [InlineKeyboardButton(text, callback_data=f"react_task_{task.id}")]
    elif task.type == "boost":
        text = f"⚡ Буст: {task.target_name} | {task.reward}{CURRENCY}"
        return [InlineKeyboardButton(text, callback_data=f"boost_task_{task.id}")]
    return []

# ----------------------------- ОСНОВНОЙ БОТ -----------------------------
class PromoBot:
    def __init__(self):
        self.scheduler = AsyncIOScheduler()
        self.user_states = {}  # для временных данных

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        # обработка реферальной ссылки
        args = context.args
        ref_code = args[0] if args and args[0].startswith("ref_") else None
        session = Session()
        db_user = session.query(User).filter_by(user_id=user.id).first()
        if not db_user:
            referrer = None
            if ref_code:
                referrer = session.query(User).filter_by(referral_code=ref_code).first()
            new_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
            db_user = User(user_id=user.id, username=user.username, first_name=user.first_name,
                           referral_code=new_code, referred_by=referrer.user_id if referrer else None)
            session.add(db_user)
            session.commit()
            if referrer:
                # бонус рефереру
                referrer.balance += REFERRAL_BONUS
                add_transaction(referrer.user_id, REFERRAL_BONUS, "referral", f"Приглашён {user.id}")
                session.commit()
        session.close()

        await update.message.reply_text(
            f"✨ *Добро пожаловать в бот взаимного пиара!*\n\n"
            f"Здесь вы можете:\n"
            f"💰 *Зарабатывать* {CURRENCY}, выполняя задания\n"
            f"📢 *Рекламировать* свои каналы, группы, посты\n"
            f"💸 *Создавать чеки* для перевода монет\n"
            f"📊 *Следить за статистикой* в личном кабинете\n\n"
            f"Ваш ID: `{user.id}`\n"
            f"Реферальная ссылка: https://t.me/{context.bot.username}?start=ref_{db_user.referral_code}\n\n"
            f"Используйте кнопки ниже 👇",
            parse_mode="Markdown",
            reply_markup=main_keyboard()
        )

    # ----------------------------- ОБРАБОТЧИКИ МЕНЮ -----------------------------
    async def main_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        user_id = query.from_user.id
        user = get_user(user_id)
        await query.edit_message_text(
            f"Главное меню\n\n💰 Баланс: {user.balance} {CURRENCY}\nУровень: {user.level} (XP: {user.xp}/{user.level*XP_LEVEL_BASE})",
            reply_markup=main_keyboard()
        )

    async def earn_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        # показать статистику количества заданий по типам
        session = Session()
        counts = {}
        for t in ["channel", "group", "post", "reaction", "bot", "boost"]:
            cnt = session.query(Task).filter(Task.type == t, Task.is_active == True, Task.is_paused == False).count()
            counts[t] = cnt
        session.close()
        text = (
            f"👨‍💻 *Заработать*\n\n"
            f"📢 Заданий на каналы: {counts.get('channel',0)}\n"
            f"👤 Заданий на группы: {counts.get('group',0)}\n"
            f"👁 Заданий на просмотр: {counts.get('post',0)}\n"
            f"🤖 Заданий на боты: {counts.get('bot',0)}\n"
            f"⚡ Заданий на бусты: {counts.get('boost',0)}\n"
            f"🔥 Заданий на реакции: {counts.get('reaction',0)}\n\n"
            f"🔔 Выберите способ заработка 👇"
        )
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=earn_type_keyboard())

    async def show_tasks_by_type(self, update: Update, context: ContextTypes.DEFAULT_TYPE, task_type: str):
        query = update.callback_query
        await query.answer()
        page = context.user_data.get(f"{task_type}_page", 0)
        session = Session()
        tasks = session.query(Task).filter(
            Task.type == task_type,
            Task.is_active == True,
            Task.is_paused == False,
            Task.current_completions < Task.max_completions,
            Task.expires_at > datetime.now()
        ).order_by(Task.reward.desc()).all()
        total_pages = (len(tasks) + MAX_TASKS_PER_PAGE - 1) // MAX_TASKS_PER_PAGE
        start = page * MAX_TASKS_PER_PAGE
        tasks_page = tasks[start:start+MAX_TASKS_PER_PAGE]

        if not tasks_page:
            await query.edit_message_text("Нет заданий этого типа.", reply_markup=back_keyboard("earn"))
            session.close()
            return

        # сообщение с предупреждением (для каналов/групп)
        if task_type in ("channel", "group", "bot"):
            text = ("⚠️ *Запрещено отписываться ранее чем через 7 дней от каналов/групп/ботов*\n"
                    "В противном случае ваша возможность выполнять задания будет заблокирована, "
                    "а заработанные средства аннулированы.\n\n")
        else:
            text = ""

        # строим кнопки: для каждого задания – ряд из двух кнопок (ссылка + проверить) или одной
        keyboard = []
        for task in tasks_page:
            if task_type in ("channel", "group", "bot"):
                # ссылка и кнопка проверки
                url = task.extra_data if task.extra_data else f"https://t.me/{task.target_name}"
                keyboard.append([
                    InlineKeyboardButton(f"💰 {task.reward}{CURRENCY} | {task.target_name}", url=url),
                    InlineKeyboardButton("✅ Проверить", callback_data=f"verify_sub_{task.id}")
                ])
            elif task_type == "post":
                keyboard.append([InlineKeyboardButton(f"👁 Просмотр: {task.target_name} | {task.reward}{CURRENCY}",
                                                     callback_data=f"do_view_{task.id}")])
            elif task_type == "reaction":
                keyboard.append([InlineKeyboardButton(f"🔥 Реакция: {task.target_name} | {task.reward}{CURRENCY}",
                                                     callback_data=f"do_reaction_{task.id}")])
            elif task_type == "boost":
                keyboard.append([InlineKeyboardButton(f"⚡ Буст: {task.target_name} | {task.reward}{CURRENCY}",
                                                     callback_data=f"do_boost_{task.id}")])
        # пагинация
        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton("◀️", callback_data=f"tasks_{task_type}_prev"))
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton("▶️", callback_data=f"tasks_{task_type}_next"))
        if nav_buttons:
            keyboard.append(nav_buttons)
        keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="earn")])

        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        session.close()

    async def verify_subscription(self, update: Update, context: ContextTypes.DEFAULT_TYPE, task_id: int):
        query = update.callback_query
        user_id = query.from_user.id
        session = Session()
        task = session.query(Task).filter_by(id=task_id, is_active=True).first()
        if not task:
            await query.answer("Задание не найдено или уже неактивно")
            session.close()
            return
        # проверяем, не выполнял ли уже
        existing = session.query(TaskCompletion).filter_by(task_id=task_id, user_id=user_id).first()
        if existing:
            await query.answer("Вы уже выполняли это задание", show_alert=True)
            session.close()
            return
        # проверка подписки
        try:
            if task.type == "channel" or task.type == "group":
                member = await context.bot.get_chat_member(chat_id=task.target_id, user_id=user_id)
                if member.status not in ("member", "administrator", "creator"):
                    await query.answer("❌ Вы не подписаны на канал/группу!", show_alert=True)
                    session.close()
                    return
            elif task.type == "bot":
                # для бота – проверяем, запустил ли пользователь бота (через start)
                # упрощённо: считаем, что если пользователь нажал "Проверить", он уже запустил
                pass
            else:
                await query.answer("Неподдерживаемый тип", show_alert=True)
                session.close()
                return
        except BadRequest:
            await query.answer("Не удалось проверить подписку. Возможно, бот не администратор или канал скрыт.", show_alert=True)
            session.close()
            return

        # начисляем награду
        user = session.query(User).filter_by(user_id=user_id).first()
        reward = task.reward
        user.balance += reward
        user.earned_balance += reward
        add_transaction(user_id, reward, "task_reward", f"Выполнение {task.type} {task.target_name}")
        # записываем выполнение
        completion = TaskCompletion(task_id=task_id, user_id=user_id, is_verified=True, completed_at=datetime.now())
        session.add(completion)
        task.current_completions += 1
        # запоминаем подписку для отслеживания отписки
        if task.type in ("channel", "group"):
            sub = Subscription(user_id=user_id, channel_id=task.target_id, task_id=task_id,
                               check_until=datetime.now() + timedelta(days=UNSUBSCRIBE_BAN_DAYS))
            session.add(sub)
        session.commit()
        # начисляем XP
        add_xp(user_id, XP_PER_TASK)

        await query.edit_message_text(
            f"✅ Подписка подтверждена! Вы получили {reward} {CURRENCY}\n"
            f"Новый баланс: {user.balance} {CURRENCY}\n"
            f"⚠️ Напоминаем: нельзя отписываться в течение 7 дней."
        )
        session.close()

    async def handle_view_task(self, update: Update, context: ContextTypes.DEFAULT_TYPE, task_id: int):
        query = update.callback_query
        user_id = query.from_user.id
        session = Session()
        task = session.query(Task).filter_by(id=task_id, is_active=True).first()
        if not task:
            await query.answer("Задание неактивно", show_alert=True)
            session.close()
            return
        existing = session.query(TaskCompletion).filter_by(task_id=task_id, user_id=user_id).first()
        if existing:
            await query.answer("Вы уже выполняли это задание", show_alert=True)
            session.close()
            return
        # сохраняем состояние – ожидаем скриншот
        context.user_data[f"pending_view_{user_id}"] = task_id
        await query.edit_message_text(
            f"👁 *Задание на просмотр*\n\n"
            f"Перейдите по ссылке и посмотрите пост:\n{task.extra_data}\n\n"
            f"После просмотра отправьте скриншот (экрана, чтобы было видно пост) в этот чат.\n"
            f"Ваша награда: {task.reward} {CURRENCY}\n\n"
            f"После отправки скриншота владелец задания проверит его в течение 24 часов.\n"
            f"Если за это время проверки не будет – награда зачислится автоматически.",
            parse_mode="Markdown",
            reply_markup=back_keyboard("earn")
        )
        session.close()

    async def handle_reaction_task(self, update: Update, context: ContextTypes.DEFAULT_TYPE, task_id: int):
        query = update.callback_query
        user_id = query.from_user.id
        session = Session()
        task = session.query(Task).filter_by(id=task_id, is_active=True).first()
        if not task:
            await query.answer("Задание неактивно", show_alert=True)
            session.close()
            return
        existing = session.query(TaskCompletion).filter_by(task_id=task_id, user_id=user_id).first()
        if existing:
            await query.answer("Вы уже выполняли это задание", show_alert=True)
            session.close()
            return
        context.user_data[f"pending_reaction_{user_id}"] = task_id
        await query.edit_message_text(
            f"🔥 *Задание на реакцию*\n\n"
            f"Перейдите по ссылке на пост:\n{task.extra_data}\n\n"
            f"Поставьте реакцию *(лайк, сердечко и т.д.)* и сделайте скриншот, где видна ваша реакция.\n"
            f"Отправьте скриншот в этот чат.\n\n"
            f"Награда: {task.reward} {CURRENCY}\n\n"
            f"Владелец проверит и одобрит или отправит на доработку.\n"
            f"Если через 24 часа не проверит – награда автоматически зачислится.",
            parse_mode="Markdown",
            reply_markup=back_keyboard("earn")
        )
        session.close()

    async def handle_boost_task(self, update: Update, context: ContextTypes.DEFAULT_TYPE, task_id: int):
        # буст канала – аналогично подписке, но проверка сложнее; упростим: пользователь вручную ставит буст и отправляет скриншот
        query = update.callback_query
        user_id = query.from_user.id
        session = Session()
        task = session.query(Task).filter_by(id=task_id, is_active=True).first()
        if not task:
            await query.answer("Задание неактивно", show_alert=True)
            session.close()
            return
        context.user_data[f"pending_boost_{user_id}"] = task_id
        await query.edit_message_text(
            f"⚡ *Премиум буст*\n\n"
            f"Сделайте буст канала: https://t.me/{task.target_name}\n\n"
            f"После того, как буст будет активирован, отправьте скриншот подтверждения (можно из настроек канала).\n"
            f"Награда: {task.reward} {CURRENCY}",
            parse_mode="Markdown",
            reply_markup=back_keyboard("earn")
        )
        session.close()

    # ----------------------------- ПРИЁМ СКРИНШОТОВ -----------------------------
    async def handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        pending = None
        task_id = None
        for key in list(context.user_data.keys()):
            if key.startswith("pending_") and key.endswith(str(user_id)):
                pending = key
                task_id = context.user_data[key]
                break
        if not task_id:
            await update.message.reply_text("Я не ожидал от вас скриншот. Пожалуйста, начните задание заново через меню.")
            return
        session = Session()
        task = session.query(Task).filter_by(id=task_id).first()
        if not task:
            await update.message.reply_text("Задание уже неактивно.")
            session.close()
            return
        # создаём запись о выполнении, но неподтверждённое
        completion = TaskCompletion(task_id=task_id, user_id=user_id, is_verified=False,
                                    screenshot_message_id=update.message.message_id)
        session.add(completion)
        session.commit()
        # отправляем скриншот владельцу задания
        owner_id = task.creator_id
        try:
            caption = f"📸 Новое выполнение задания #{task_id} от пользователя @{update.effective_user.username or user_id}\n\nНаграда: {task.reward}{CURRENCY}"
            # копируем фото в личку владельцу
            await context.bot.send_photo(chat_id=owner_id, photo=update.message.photo[-1].file_id,
                                         caption=caption,
                                         reply_markup=InlineKeyboardMarkup([
                                             [InlineKeyboardButton("✅ Одобрить", callback_data=f"approve_{completion.id}"),
                                              InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{completion.id}")]
                                         ]))
            await update.message.reply_text("✅ Скриншот отправлен на проверку владельцу. Ожидайте подтверждения. Если проверки не будет в течение 24 часов, награда зачислится автоматически.")
        except Exception as e:
            await update.message.reply_text("Не удалось отправить скриншот владельцу. Попробуйте позже.")
        session.close()
        # очищаем состояние
        del context.user_data[pending]

    async def approve_completion(self, update: Update, context: ContextTypes.DEFAULT_TYPE, completion_id: int):
        query = update.callback_query
        await query.answer()
        session = Session()
        completion = session.query(TaskCompletion).filter_by(id=completion_id).first()
        if not completion:
            await query.edit_message_text("Запись не найдена")
            session.close()
            return
        if completion.is_verified:
            await query.edit_message_text("Уже одобрено")
            session.close()
            return
        task = session.query(Task).filter_by(id=completion.task_id).first()
        if not task:
            await query.edit_message_text("Задание не найдено")
            session.close()
            return
        # начисляем награду
        user = session.query(User).filter_by(user_id=completion.user_id).first()
        reward = task.reward
        user.balance += reward
        user.earned_balance += reward
        add_transaction(completion.user_id, reward, "task_reward", f"Выполнение {task.type} (одобрено)")
        completion.is_verified = True
        completion.approved_at = datetime.now()
        task.current_completions += 1
        # подписка для отслеживания
        if task.type in ("channel", "group"):
            sub = Subscription(user_id=completion.user_id, channel_id=task.target_id, task_id=task.id,
                               check_until=datetime.now() + timedelta(days=UNSUBSCRIBE_BAN_DAYS))
            session.add(sub)
        session.commit()
        add_xp(completion.user_id, XP_PER_TASK)
        await query.edit_message_text(f"✅ Выполнение одобрено! Пользователь получил {reward}{CURRENCY}.")
        # уведомление исполнителю
        try:
            await context.bot.send_message(completion.user_id, f"✅ Ваше выполнение задания #{task.id} одобрено. Начислено {reward}{CURRENCY}.")
        except:
            pass
        session.close()

    async def reject_completion(self, update: Update, context: ContextTypes.DEFAULT_TYPE, completion_id: int):
        query = update.callback_query
        await query.answer()
        session = Session()
        completion = session.query(TaskCompletion).filter_by(id=completion_id).first()
        if not completion:
            await query.edit_message_text("Запись не найдена")
            session.close()
            return
        if completion.is_verified:
            await query.edit_message_text("Уже одобрено, отклонение невозможно")
            session.close()
            return
        # удаляем запись, чтобы пользователь мог попробовать снова
        session.delete(completion)
        session.commit()
        await query.edit_message_text("❌ Выполнение отклонено. Пользователь может попробовать снова.")
        try:
            await context.bot.send_message(completion.user_id, f"❌ Ваше выполнение задания #{completion.task_id} отклонено. Пожалуйста, повторите попытку.")
        except:
            pass
        session.close()

    # ----------------------------- СОЗДАНИЕ ЗАДАНИЙ (РЕКЛАМИРОВАТЬ) -----------------------------
    async def advertise_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        user_id = query.from_user.id
        user = get_user(user_id)
        text = f"Что вы хотите рекламировать?\n\n💰 Баланс: {user.balance} {CURRENCY}"
        keyboard = [
            [InlineKeyboardButton("📺 Канал", callback_data="ad_channel")],
            [InlineKeyboardButton("👥 Группа", callback_data="ad_group")],
            [InlineKeyboardButton("👁 Пост", callback_data="ad_post")],
            [InlineKeyboardButton("🤖 Бот", callback_data="ad_bot")],
            [InlineKeyboardButton("⚡ Премиум буст", callback_data="ad_boost")],
            [InlineKeyboardButton("🔥 Реакции", callback_data="ad_reaction")],
            [InlineKeyboardButton("📋 Мои задания", callback_data="my_tasks")],
            [InlineKeyboardButton("🔙 Назад", callback_data="main_menu")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

    async def start_ad_creation(self, update: Update, context: ContextTypes.DEFAULT_TYPE, ad_type: str):
        query = update.callback_query
        await query.answer()
        context.user_data["ad_type"] = ad_type
        await query.edit_message_text("Введите ссылку на ресурс (например, https://t.me/username или @username):",
                                      reply_markup=back_keyboard("advertise"))
        return "awaiting_target"

    async def receive_target(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        ad_type = context.user_data.get("ad_type")
        if not ad_type:
            await update.message.reply_text("Пожалуйста, начните создание задания заново через меню Рекламировать.")
            return ConversationHandler.END
        target = update.message.text.strip()
        # дальнейшая обработка: проверка прав бота для каналов/групп, выделение имени
        if ad_type in ("channel", "group"):
            # извлекаем username
            if target.startswith("https://t.me/"):
                username = target.split("https://t.me/")[-1].replace("/", "")
            elif target.startswith("@"):
                username = target[1:]
            else:
                username = target
            # пытаемся получить информацию о чате, проверить бота админом
            try:
                chat = await context.bot.get_chat(username)
                member = await context.bot.get_chat_member(chat.id, context.bot.id)
                if member.status not in ("administrator", "creator"):
                    await update.message.reply_text("❌ Бот не является администратором канала/группы. Добавьте бота и выдайте права, затем повторите.")
                    return ConversationHandler.END
                link = await context.bot.create_chat_invite_link(chat.id, creates_join_request=True)
                invite_link = link.invite_link
            except Exception as e:
                await update.message.reply_text(f"Не удалось проверить бота: {str(e)}. Убедитесь, что бот добавлен и имеет права администратора.")
                return ConversationHandler.END
            context.user_data["target_id"] = str(chat.id)
            context.user_data["target_name"] = chat.title
            context.user_data["extra_data"] = invite_link
        else:
            # для постов, ботов и т.д.
            context.user_data["target_name"] = target
            context.user_data["extra_data"] = target
        await update.message.reply_text("Введите награду за выполнение (целое число, от 1 до 1000):")
        return "awaiting_reward"

    async def receive_reward(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            reward = int(update.message.text)
            if reward < 1 or reward > 1000:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Введите число от 1 до 1000.")
            return "awaiting_reward"
        context.user_data["reward"] = reward
        await update.message.reply_text("Введите максимальное количество выполнений (1-1000):")
        return "awaiting_max"

    async def receive_max(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            max_completions = int(update.message.text)
            if max_completions < 1 or max_completions > 1000:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Введите число от 1 до 1000.")
            return "awaiting_max"
        ad_type = context.user_data.get("ad_type")
        reward = context.user_data["reward"]
        # расчёт суммы списания
        total_needed = reward * max_completions
        user = get_user(update.effective_user.id)
        # комиссия, если используются заработанные монеты
        if user.balance < total_needed:
            await update.message.reply_text(f"Недостаточно средств. Нужно {total_needed} {CURRENCY}, у вас {user.balance}.")
            return ConversationHandler.END
        commission = 0
        if user.balance - user.earned_balance < total_needed:
            # не хватает купленных монет, значит часть списывается из заработанных – комиссия
            commission = int(total_needed * COMMISSION_RATE)
            total_needed += commission
            if user.balance < total_needed:
                await update.message.reply_text(f"Недостаточно средств с учётом комиссии 10%. Нужно {total_needed} {CURRENCY}.")
                return ConversationHandler.END
        # списываем
        update_user_balance(update.effective_user.id, -total_needed)
        add_transaction(update.effective_user.id, -total_needed, "task_creation",
                        f"Создание задания {ad_type} на {max_completions} выполнений")
        # создаём задание
        session = Session()
        task = Task(
            type=ad_type,
            target_id=context.user_data.get("target_id", ""),
            target_name=context.user_data["target_name"],
            reward=reward,
            max_completions=max_completions,
            current_completions=0,
            creator_id=update.effective_user.id,
            expires_at=datetime.now() + timedelta(days=TASK_DURATION_DAYS),
            extra_data=context.user_data["extra_data"]
        )
        session.add(task)
        session.commit()
        session.close()
        await update.message.reply_text(
            f"✅ Задание создано!\n"
            f"Тип: {ad_type}\n"
            f"Цель: {context.user_data['target_name']}\n"
            f"Награда: {reward}{CURRENCY}\n"
            f"Макс. выполнений: {max_completions}\n"
            f"Комиссия: {commission}{CURRENCY}\n"
            f"Списано с баланса: {total_needed}{CURRENCY}"
        )
        context.user_data.clear()
        return ConversationHandler.END

    # ----------------------------- ЧЕКИ -----------------------------
    async def checks_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        keyboard = [
            [InlineKeyboardButton("💳 Персональный чек", callback_data="create_personal_check")],
            [InlineKeyboardButton("👥 Мульти-чек", callback_data="create_multi_check")],
            [InlineKeyboardButton("🔙 Назад", callback_data="main_menu")]
        ]
        await query.edit_message_text(
            "Чеки позволяют отправлять монеты прямо в сообщениях.\n\n"
            "👉 Персональный чек – для одного пользователя.\n"
            "👉 Мульти‑чек – для нескольких (с условием подписки).\n\n"
            "Выберите тип:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    async def create_personal_check(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        context.user_data["check_type"] = "personal"
        await query.edit_message_text("Введите сумму чека (целое число):", reply_markup=back_keyboard("checks_menu"))
        return "await_check_amount"

    async def create_multi_check(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        context.user_data["check_type"] = "multi"
        await query.edit_message_text("Введите сумму для каждого получателя (целое число):", reply_markup=back_keyboard("checks_menu"))
        return "await_check_amount"

    async def receive_check_amount(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            amount = int(update.message.text)
            if amount < 1:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Введите положительное целое число.")
            return "await_check_amount"
        context.user_data["check_amount"] = amount
        check_type = context.user_data["check_type"]
        if check_type == "personal":
            await update.message.reply_text("Введите ID получателя (число):")
            return "await_personal_recipient"
        else:  # multi
            await update.message.reply_text("Введите количество получателей (макс 100):")
            return "await_multi_count"

    async def receive_personal_recipient(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            recipient_id = int(update.message.text)
        except ValueError:
            await update.message.reply_text("ID должен быть числом.")
            return "await_personal_recipient"
        amount = context.user_data["check_amount"]
        user_id = update.effective_user.id
        session = Session()
        user = session.query(User).filter_by(user_id=user_id).first()
        if user.balance < amount:
            await update.message.reply_text(f"Недостаточно средств. Баланс: {user.balance} {CURRENCY}")
            session.close()
            return ConversationHandler.END
        # создаём чек
        code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=10))
        new_check = Check(
            type="personal",
            owner_id=user_id,
            amount=amount,
            total_amount=amount,
            remaining=amount,
            max_uses=1,
            code=code
        )
        session.add(new_check)
        user.balance -= amount
        add_transaction(user_id, -amount, "check_creation", f"Создание персонального чека на {amount}{CURRENCY}")
        session.commit()
        session.close()
        await update.message.reply_text(
            f"✅ Персональный чек создан!\n"
            f"Сумма: {amount} {CURRENCY}\n"
            f"Код: `{code}`\n"
            f"Отправьте его получателю. Получатель должен ввести команду /claim {code}",
            parse_mode="Markdown"
        )
        context.user_data.clear()
        return ConversationHandler.END

    async def receive_multi_count(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            count = int(update.message.text)
            if count < 1 or count > 100:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Введите число от 1 до 100.")
            return "await_multi_count"
        context.user_data["multi_count"] = count
        await update.message.reply_text("Введите ссылку на канал, подписка на который обязательна (или оставьте 0, если без условия):")
        return "await_multi_channel"

    async def receive_multi_channel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        channel_input = update.message.text.strip()
        required_channel = None
        if channel_input != "0":
            # извлекаем ID канала, проверяем, что бот админ
            try:
                if channel_input.startswith("https://t.me/"):
                    username = channel_input.split("https://t.me/")[-1].replace("/", "")
                elif channel_input.startswith("@"):
                    username = channel_input[1:]
                else:
                    username = channel_input
                chat = await context.bot.get_chat(username)
                member = await context.bot.get_chat_member(chat.id, context.bot.id)
                if member.status not in ("administrator", "creator"):
                    await update.message.reply_text("❌ Бот не администратор указанного канала. Добавьте бота и выдайте права, либо пропустите условие (0).")
                    return "await_multi_channel"
                required_channel = str(chat.id)
            except Exception:
                await update.message.reply_text("Не удалось проверить канал. Пропустите условие, введя 0.")
                return "await_multi_channel"
        amount = context.user_data["check_amount"]
        count = context.user_data["multi_count"]
        total = amount * count
        user_id = update.effective_user.id
        session = Session()
        user = session.query(User).filter_by(user_id=user_id).first()
        if user.balance < total:
            await update.message.reply_text(f"Недостаточно средств. Нужно {total} {CURRENCY}")
            session.close()
            return ConversationHandler.END
        code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=10))
        new_check = Check(
            type="multi",
            owner_id=user_id,
            amount=amount,
            total_amount=total,
            remaining=total,
            max_uses=count,
            required_channel=required_channel,
            code=code
        )
        session.add(new_check)
        user.balance -= total
        add_transaction(user_id, -total, "check_creation", f"Создание мульти-чека на {count} получателей")
        session.commit()
        session.close()
        await update.message.reply_text(
            f"✅ Мульти-чек создан!\n"
            f"Сумма на каждого: {amount} {CURRENCY}\n"
            f"Всего: {count} получателей\n"
            f"Условие подписки: {'канал @' + required_channel if required_channel else 'нет'}\n"
            f"Код: `{code}`\n"
            f"Получатели могут активировать его командой /claim {code}\n"
            f"Чек будет действовать до тех пор, пока не будут использованы все {count} раз.",
            parse_mode="Markdown"
        )
        context.user_data.clear()
        return ConversationHandler.END

    async def claim_check(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        args = context.args
        if not args:
            await update.message.reply_text("Использование: /claim <код_чека>")
            return
        code = args[0]
        user_id = update.effective_user.id
        session = Session()
        check = session.query(Check).filter_by(code=code, is_active=True).first()
        if not check:
            await update.message.reply_text("❌ Чек не найден или уже неактивен.")
            session.close()
            return
        if check.used_count >= check.max_uses:
            await update.message.reply_text("❌ Чек уже полностью использован.")
            session.close()
            return
        # проверка условия подписки
        if check.required_channel:
            try:
                member = await context.bot.get_chat_member(chat_id=check.required_channel, user_id=user_id)
                if member.status not in ("member", "administrator", "creator"):
                    await update.message.reply_text(f"❌ Вы не подписаны на необходимый канал. Подпишитесь и попробуйте снова.")
                    session.close()
                    return
            except BadRequest:
                await update.message.reply_text("Не удалось проверить подписку. Возможно, канал недоступен.")
                session.close()
                return
        # начисляем
        user = session.query(User).filter_by(user_id=user_id).first()
        if not user:
            user = User(user_id=user_id)  # создаём, если нет
            session.add(user)
            session.flush()
        user.balance += check.amount
        add_transaction(user_id, check.amount, "check_reward", f"Получение чека {code}")
        check.used_count += 1
        check.remaining -= check.amount
        if check.used_count >= check.max_uses:
            check.is_active = False
        session.commit()
        await update.message.reply_text(f"✅ Вы получили {check.amount} {CURRENCY} по чеку {code}!")
        session.close()

    # ----------------------------- КАБИНЕТ -----------------------------
    async def cabinet(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        user_id = query.from_user.id
        user = get_user(user_id)
        session = Session()
        earned = user.earned_balance
        spent_on_tasks = session.query(Transaction).filter(Transaction.user_id == user_id, Transaction.type == "task_creation").with_entities(func.sum(Transaction.amount)).scalar() or 0
        received_from_tasks = session.query(Transaction).filter(Transaction.user_id == user_id, Transaction.type == "task_reward").with_entities(func.sum(Transaction.amount)).scalar() or 0
        level_xp_needed = user.level * XP_LEVEL_BASE
        text = (
            f"👤 *Личный кабинет*\n\n"
            f"🆔 ID: `{user_id}`\n"
            f"⭐ Уровень: {user.level} (XP: {user.xp}/{level_xp_needed})\n"
            f"💰 Баланс: {user.balance} {CURRENCY}\n"
            f"💵 Заработано всего: {received_from_tasks} {CURRENCY}\n"
            f"📉 Потрачено на рекламу: {spent_on_tasks} {CURRENCY}\n\n"
            f"📈 *Реферальная система*\n"
            f"Ваша ссылка: `https://t.me/{context.bot.username}?start=ref_{user.referral_code}`"
        )
        keyboard = [
            [InlineKeyboardButton("📈 Пополнить баланс", callback_data="deposit")],
            [InlineKeyboardButton("💬 Реферальная система", callback_data="referral_info")],
            [InlineKeyboardButton("✅ Уровневая система", callback_data="level_info")],
            [InlineKeyboardButton("🟢 Мои задания", callback_data="my_tasks")],
            [InlineKeyboardButton("✖ Изменить язык", callback_data="change_lang")],
            [InlineKeyboardButton("❌ Отключить уведомления", callback_data="toggle_notify")],
            [InlineKeyboardButton("🔙 Назад", callback_data="main_menu")]
        ]
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        session.close()

    async def my_tasks(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        user_id = query.from_user.id
        session = Session()
        tasks = session.query(Task).filter_by(creator_id=user_id).order_by(Task.created_at.desc()).all()
        if not tasks:
            await query.edit_message_text("У вас нет созданных заданий.", reply_markup=back_keyboard("cabinet"))
            session.close()
            return
        text = "📋 *Ваши задания:*\n\n"
        buttons = []
        for t in tasks:
            status = "🟢 Активно" if (t.is_active and not t.is_paused and t.expires_at > datetime.now()) else ("🔴 Завершено" if t.expires_at <= datetime.now() else "⏸ Приостановлено")
            text += f"• {t.type} {t.target_name} | {t.reward}{CURRENCY} | {t.current_completions}/{t.max_completions} | {status}\n"
            if t.is_active:
                buttons.append([InlineKeyboardButton(f"❌ Удалить {t.target_name}", callback_data=f"delete_task_{t.id}")])
        if buttons:
            buttons.append([InlineKeyboardButton("🔙 Назад", callback_data="cabinet")])
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))
        else:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=back_keyboard("cabinet"))
        session.close()

    async def delete_task(self, update: Update, context: ContextTypes.DEFAULT_TYPE, task_id: int):
        query = update.callback_query
        user_id = query.from_user.id
        session = Session()
        task = session.query(Task).filter_by(id=task_id, creator_id=user_id).first()
        if not task:
            await query.answer("Задание не найдено", show_alert=True)
            session.close()
            return
        if not task.is_active:
            await query.answer("Задание уже неактивно", show_alert=True)
            session.close()
            return
        # возврат средств
        remaining = task.max_completions - task.current_completions
        refund = task.reward * remaining
        # комиссия не возвращается
        user = session.query(User).filter_by(user_id=user_id).first()
        user.balance += refund
        add_transaction(user_id, refund, "refund", f"Возврат за удаление задания {task.id}")
        task.is_active = False
        session.commit()
        await query.edit_message_text(f"Задание удалено. Возвращено {refund} {CURRENCY}.")
        session.close()

    # ----------------------------- АВТОМАТИЧЕСКИЕ ЗАДАЧИ -----------------------------
    async def check_unsubscribes(self):
        # проверяем пользователей, которые отписались от каналов раньше 7 дней
        session = Session()
        now = datetime.now()
        subs = session.query(Subscription).filter(Subscription.check_until > now).all()
        for sub in subs:
            try:
                member = await self.bot.get_chat_member(chat_id=sub.channel_id, user_id=sub.user_id)
                if member.status not in ("member", "administrator", "creator"):
                    # отписался – блокируем
                    user = session.query(User).filter_by(user_id=sub.user_id).first()
                    user.is_banned = True
                    user.ban_until = now + timedelta(days=UNSUBSCRIBE_BAN_DAYS)
                    session.commit()
                    # также списываем награду (можно вычесть из баланса)
                    task = session.query(Task).filter_by(id=sub.task_id).first()
                    if task:
                        user.balance -= task.reward
                        add_transaction(sub.user_id, -task.reward, "penalty", "Отписка в течение 7 дней")
                    await self.bot.send_message(sub.user_id, f"Вы были заблокированы за отписку ранее 7 дней. Разблокировка {user.ban_until.strftime('%d.%m.%Y')}")
            except Exception:
                pass
        session.close()

    async def auto_approve_tasks(self):
        # задания, где скриншот не проверен более 24 часов
        session = Session()
        timeout = datetime.now() - timedelta(hours=AUTO_APPROVE_HOURS)
        completions = session.query(TaskCompletion).filter(
            TaskCompletion.is_verified == False,
            TaskCompletion.completed_at < timeout
        ).all()
        for comp in completions:
            task = session.query(Task).filter_by(id=comp.task_id).first()
            if task and task.is_active:
                # начисляем автоматически
                user = session.query(User).filter_by(user_id=comp.user_id).first()
                user.balance += task.reward
                user.earned_balance += task.reward
                add_transaction(comp.user_id, task.reward, "task_reward", f"Автоодобрение задания {task.id}")
                comp.is_verified = True
                comp.approved_at = datetime.now()
                task.current_completions += 1
                session.commit()
                await self.bot.send_message(comp.user_id, f"✅ Ваше задание #{task.id} автоматически одобрено через {AUTO_APPROVE_HOURS} часов. Получено {task.reward}{CURRENCY}.")
        session.close()

    async def clean_expired_tasks(self):
        session = Session()
        now = datetime.now()
        expired = session.query(Task).filter(Task.expires_at < now, Task.is_active == True).all()
        for task in expired:
            task.is_active = False
            # возврат средств не нужен, так как задание истекло, деньги уже потрачены
        session.commit()
        session.close()

    async def daily_bonus(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        user_id = query.from_user.id
        session = Session()
        user = session.query(User).filter_by(user_id=user_id).first()
        now = datetime.now()
        if user.last_daily and (now - user.last_daily).days < 1:
            hours_left = 24 - (now - user.last_daily).seconds // 3600
            await query.edit_message_text(f"⏳ Ежедневный бонус уже получен. Следующий через {hours_left} ч.")
            session.close()
            return
        streak = user.daily_streak + 1 if user.last_daily and (now - user.last_daily).days == 1 else 1
        bonus = DAILY_BONUS_AMOUNT + 10 * min(streak, 30)   # прогрессивный бонус
        user.balance += bonus
        user.daily_streak = streak
        user.last_daily = now
        add_transaction(user_id, bonus, "daily", f"Ежедневный бонус (серия {streak})")
        session.commit()
        await query.edit_message_text(f"🎁 Ежедневный бонус: +{bonus} {CURRENCY}\n📅 Серия: {streak} дней")
        session.close()

    # ----------------------------- ЗАПУСК БОТА -----------------------------
    async def set_commands(self, app: Application):
        commands = [
            ("start", "Главное меню"),
            ("help", "Помощь"),
            ("stats", "Статистика"),
            ("rules", "Правила"),
            ("support", "Поддержка")
        ]
        await app.bot.set_my_commands([BotCommand(cmd, desc) for cmd, desc in commands])

    def run(self):
        app = Application.builder().token(BOT_TOKEN).build()
        # регистрация обработчиков
        app.add_handler(CommandHandler("start", self.start))
        app.add_handler(CommandHandler("claim", self.claim_check))
        app.add_handler(CommandHandler("help", lambda u,c: u.message.reply_text("Используйте меню бота для навигации.")))
        app.add_handler(CommandHandler("stats", self.stats_command))
        app.add_handler(CommandHandler("rules", lambda u,c: u.message.reply_text(f"Правила: {RULES_LINK}")))
        app.add_handler(CommandHandler("support", lambda u,c: u.message.reply_text(f"Поддержка: {SUPPORT_LINK}")))

        # обработчики кнопок
        app.add_handler(CallbackQueryHandler(self.main_menu, pattern="^main_menu$"))
        app.add_handler(CallbackQueryHandler(self.earn_menu, pattern="^earn$"))
        app.add_handler(CallbackQueryHandler(self.advertise_menu, pattern="^advertise$"))
        app.add_handler(CallbackQueryHandler(self.checks_menu, pattern="^checks_menu$"))
        app.add_handler(CallbackQueryHandler(self.cabinet, pattern="^cabinet$"))

        # типы заработать
        for t in ["channels", "groups", "views", "reactions", "bots", "boost"]:
            app.add_handler(CallbackQueryHandler(lambda u,c: self.show_tasks_by_type(u,c,task_type=t), pattern=f"^earn_{t}$"))

        # пагинация заданий
        for tt in ["channel", "group", "post", "reaction", "bot", "boost"]:
            app.add_handler(CallbackQueryHandler(lambda u,c: self.paginate_tasks(u,c,tt,"next"), pattern=f"^tasks_{tt}_next$"))
            app.add_handler(CallbackQueryHandler(lambda u,c: self.paginate_tasks(u,c,tt,"prev"), pattern=f"^tasks_{tt}_prev$"))

        # проверка подписки, просмотр, реакция, буст
        app.add_handler(CallbackQueryHandler(lambda u,c: self.verify_subscription(u,c,int(c.data.split("_")[2])), pattern="^verify_sub_"))
        app.add_handler(CallbackQueryHandler(lambda u,c: self.handle_view_task(u,c,int(c.data.split("_")[2])), pattern="^do_view_"))
        app.add_handler(CallbackQueryHandler(lambda u,c: self.handle_reaction_task(u,c,int(c.data.split("_")[2])), pattern="^do_reaction_"))
        app.add_handler(CallbackQueryHandler(lambda u,c: self.handle_boost_task(u,c,int(c.data.split("_")[2])), pattern="^do_boost_"))

        # одобрение/отклонение
        app.add_handler(CallbackQueryHandler(lambda u,c: self.approve_completion(u,c,int(c.data.split("_")[1])), pattern="^approve_"))
        app.add_handler(CallbackQueryHandler(lambda u,c: self.reject_completion(u,c,int(c.data.split("_")[1])), pattern="^reject_"))

        # реклама – create
        for ad in ["channel","group","post","bot","boost","reaction"]:
            app.add_handler(CallbackQueryHandler(lambda u,c: self.start_ad_creation(u,c,ad), pattern=f"^ad_{ad}$"))
        app.add_handler(CallbackQueryHandler(self.my_tasks, pattern="^my_tasks$"))
        app.add_handler(CallbackQueryHandler(lambda u,c: self.delete_task(u,c,int(c.data.split("_")[2])), pattern="^delete_task_"))

        # чеки
        app.add_handler(CallbackQueryHandler(self.create_personal_check, pattern="^create_personal_check$"))
        app.add_handler(CallbackQueryHandler(self.create_multi_check, pattern="^create_multi_check$"))
        conv = ConversationHandler(
            entry_points=[],
            states={
                "await_check_amount": [MessageHandler(filters.TEXT & ~filters.COMMAND, self.receive_check_amount)],
                "await_personal_recipient": [MessageHandler(filters.TEXT & ~filters.COMMAND, self.receive_personal_recipient)],
                "await_multi_count": [MessageHandler(filters.TEXT & ~filters.COMMAND, self.receive_multi_count)],
                "await_multi_channel": [MessageHandler(filters.TEXT & ~filters.COMMAND, self.receive_multi_channel)]
            },
            fallbacks=[],
            per_message=False
        )
        app.add_handler(conv)

        # создание заданий (налоговый разговор)
        ad_conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(self.start_ad_creation, pattern="^ad_")],
            states={
                "awaiting_target": [MessageHandler(filters.TEXT & ~filters.COMMAND, self.receive_target)],
                "awaiting_reward": [MessageHandler(filters.TEXT & ~filters.COMMAND, self.receive_reward)],
                "awaiting_max": [MessageHandler(filters.TEXT & ~filters.COMMAND, self.receive_max)]
            },
            fallbacks=[],
            per_message=False
        )
        app.add_handler(ad_conv)

        # приём фото
        app.add_handler(MessageHandler(filters.PHOTO, self.handle_photo))

        # фоновая работа
        self.scheduler.add_job(self.check_unsubscribes, 'interval', hours=6)
        self.scheduler.add_job(self.auto_approve_tasks, 'interval', hours=1)
        self.scheduler.add_job(self.clean_expired_tasks, 'interval', hours=12)
        self.scheduler.start()

        app.post_init = self.set_commands

        logger = logging.getLogger(__name__)
        logger.info("Бот запущен")
        app.run_polling()

    async def stats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        session = Session()
        users_count = session.query(User).count()
        tasks_count = session.query(Task).filter(Task.is_active == True).count()
        total_balance = session.query(User).with_entities(func.sum(User.balance)).scalar() or 0
        await update.message.reply_text(
            f"📊 *Статистика бота*\n\n"
            f"👥 Пользователей: {users_count}\n"
            f"📢 Активных заданий: {tasks_count}\n"
            f"💰 Всего валюты: {total_balance} {CURRENCY}",
            parse_mode="Markdown"
        )
        session.close()

    async def paginate_tasks(self, update: Update, context: ContextTypes.DEFAULT_TYPE, task_type: str, direction: str):
        query = update.callback_query
        await query.answer()
        page_key = f"{task_type}_page"
        page = context.user_data.get(page_key, 0)
        if direction == "next":
            page += 1
        else:
            page = max(0, page - 1)
        context.user_data[page_key] = page
        await self.show_tasks_by_type(update, context, task_type)

if __name__ == "__main__":
    bot = PromoBot()
    bot.run()
