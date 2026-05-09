#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import random
import string
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean, Text, ForeignKey, BigInteger, func
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    ContextTypes, CallbackQueryHandler, ConversationHandler
)
from telegram.error import BadRequest

# ----------------------------- КОНФИГУРАЦИЯ -----------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not set")

ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(","))) if os.getenv("ADMIN_IDS") else []
SUPPORT_LINK = os.getenv("SUPPORT_LINK", "https://t.me/your_support")
RULES_LINK = os.getenv("RULES_LINK", "https://telegra.ph/Rules-05-09")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///promo_bot.db")

CURRENCY = "💷"
REFERRAL_BONUS = 100
DAILY_BONUS_AMOUNT = 50
XP_PER_TASK = 20
XP_LEVEL_BASE = 500
MAX_TASKS_PER_PAGE = 10
UNSUBSCRIBE_BAN_DAYS = 7
AUTO_APPROVE_HOURS = 24
TASK_DURATION_DAYS = 7
COMMISSION_RATE = 0.10

# ----------------------------- БАЗА ДАННЫХ -----------------------------
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, unique=True, nullable=False)
    username = Column(String)
    first_name = Column(String)
    balance = Column(Integer, default=0)
    earned_balance = Column(Integer, default=0)   # сколько заработано (для комиссии)
    referral_code = Column(String, unique=True)
    referred_by = Column(BigInteger, nullable=True)
    level = Column(Integer, default=1)
    xp = Column(Integer, default=0)
    daily_streak = Column(Integer, default=0)
    last_daily = Column(DateTime, nullable=True)
    join_date = Column(DateTime, default=datetime.now)
    is_banned = Column(Boolean, default=False)
    ban_until = Column(DateTime, nullable=True)

class Channel(Base):
    __tablename__ = "channels"
    id = Column(Integer, primary_key=True)
    channel_id = Column(String, unique=True, nullable=False)
    channel_name = Column(String)
    owner_id = Column(BigInteger, ForeignKey("users.user_id"))
    invite_link = Column(String)
    is_verified = Column(Boolean, default=False)
    added_date = Column(DateTime, default=datetime.now)

class Task(Base):
    __tablename__ = "tasks"
    id = Column(Integer, primary_key=True)
    type = Column(String, nullable=False)   # channel, group, post, bot, boost, reaction
    target_id = Column(String)              # id канала/группы
    target_name = Column(String)            # название или username
    reward = Column(Integer, nullable=False)
    max_completions = Column(Integer, default=50)
    current_completions = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    is_paused = Column(Boolean, default=False)
    creator_id = Column(BigInteger, ForeignKey("users.user_id"))
    created_at = Column(DateTime, default=datetime.now)
    expires_at = Column(DateTime, nullable=False)
    extra_data = Column(Text, nullable=True)   # ссылка на пост, тип реакции и т.д.

class TaskCompletion(Base):
    __tablename__ = "task_completions"
    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey("tasks.id"))
    user_id = Column(BigInteger, ForeignKey("users.user_id"))
    completed_at = Column(DateTime, default=datetime.now)
    is_verified = Column(Boolean, default=False)
    screenshot_message_id = Column(Integer, nullable=True)
    approved_at = Column(DateTime, nullable=True)

class Subscription(Base):
    __tablename__ = "subscriptions"
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, ForeignKey("users.user_id"))
    channel_id = Column(String, nullable=False)
    task_id = Column(Integer, ForeignKey("tasks.id"))
    subscribed_at = Column(DateTime, default=datetime.now)
    check_until = Column(DateTime, nullable=False)

class Transaction(Base):
    __tablename__ = "transactions"
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, ForeignKey("users.user_id"))
    amount = Column(Integer)
    type = Column(String)
    description = Column(Text)
    date = Column(DateTime, default=datetime.now)

class Check(Base):
    __tablename__ = "checks"
    id = Column(Integer, primary_key=True)
    type = Column(String)   # personal, multi
    owner_id = Column(BigInteger, ForeignKey("users.user_id"))
    amount = Column(Integer)
    total_amount = Column(Integer)
    remaining = Column(Integer)
    max_uses = Column(Integer, default=1)
    used_count = Column(Integer, default=0)
    required_channel = Column(String, nullable=True)
    code = Column(String, unique=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)
    expires_at = Column(DateTime, nullable=True)

# Создание движка
engine = create_engine(DATABASE_URL)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)

# ----------------------------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ -----------------------------
def get_user(user_id: int) -> User:
    session = Session()
    user = session.query(User).filter_by(user_id=user_id).first()
    if not user:
        ref_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
        user = User(user_id=user_id, referral_code=ref_code, balance=0, earned_balance=0)
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

def update_balance(user_id: int, delta: int, is_earned: bool = False):
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
        next_level_xp = user.level * XP_LEVEL_BASE
        while user.xp >= next_level_xp:
            user.level += 1
            user.xp -= next_level_xp
            level_bonus = user.level * 50
            user.balance += level_bonus
            add_transaction(user_id, level_bonus, "level_up", f"Бонус за уровень {user.level}")
            next_level_xp = user.level * XP_LEVEL_BASE
        session.commit()
    session.close()

# ----------------------------- КЛАВИАТУРЫ -----------------------------
def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Заработать", callback_data="earn")],
        [InlineKeyboardButton("📢 Рекламировать", callback_data="advertise")],
        [InlineKeyboardButton("💸 Чеки", callback_data="checks_menu")],
        [InlineKeyboardButton("👤 Мой кабинет", callback_data="cabinet")]
    ])

def back_keyboard(callback_data: str = "main_menu"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data=callback_data)]])

# ----------------------------- ОСНОВНОЙ БОТ -----------------------------
class PromoBot:
    def __init__(self):
        self.scheduler = AsyncIOScheduler()
        self.application = None

    # ----------------------------- КОМАНДЫ -----------------------------
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
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
                referrer.balance += REFERRAL_BONUS
                add_transaction(referrer.user_id, REFERRAL_BONUS, "referral", f"Приглашён {user.id}")
                session.commit()
        session.close()
        bot_username = context.bot.username or "YourBot"
        await update.message.reply_text(
            f"✨ *Добро пожаловать в бот взаимного пиара!*\n\n"
            f"Здесь вы можете:\n"
            f"💰 *Зарабатывать* {CURRENCY}, выполняя задания\n"
            f"📢 *Рекламировать* свои каналы, группы, посты\n"
            f"💸 *Создавать чеки* для перевода монет\n"
            f"📊 *Следить за статистикой* в личном кабинете\n\n"
            f"Ваш ID: `{user.id}`\n"
            f"Реферальная ссылка: https://t.me/{bot_username}?start=ref_{db_user.referral_code}\n\n"
            f"Используйте кнопки ниже 👇",
            parse_mode="Markdown",
            reply_markup=main_keyboard()
        )

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "📖 *Справка*\n\n"
            "• /start – главное меню\n"
            "• /stats – статистика бота\n"
            "• /rules – правила\n"
            "• /support – поддержка\n\n"
            "Все основные функции доступны через кнопки под сообщениями.",
            parse_mode="Markdown"
        )

    async def stats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        session = Session()
        users_count = session.query(User).count()
        active_tasks = session.query(Task).filter(Task.is_active == True, Task.expires_at > datetime.now()).count()
        total_balance = session.query(func.sum(User.balance)).scalar() or 0
        session.close()
        await update.message.reply_text(
            f"📊 *Статистика бота*\n\n"
            f"👥 Пользователей: {users_count}\n"
            f"📢 Активных заданий: {active_tasks}\n"
            f"💰 Всего монет: {total_balance} {CURRENCY}",
            parse_mode="Markdown"
        )

    async def rules_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(f"📜 *Правила использования:*\n{RULES_LINK}", parse_mode="Markdown")

    async def support_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(f"🛠 *Поддержка:* {SUPPORT_LINK}", parse_mode="Markdown")

    # ----------------------------- ОБРАБОТЧИКИ МЕНЮ -----------------------------
    async def main_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        user_id = query.from_user.id
        user = get_user(user_id)
        await query.edit_message_text(
            f"🏠 *Главное меню*\n\n💰 Баланс: {user.balance} {CURRENCY}\n⭐ Уровень: {user.level} (XP: {user.xp}/{user.level*XP_LEVEL_BASE})",
            parse_mode="Markdown",
            reply_markup=main_keyboard()
        )

    async def earn_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        session = Session()
        counts = {}
        for t in ["channel", "group", "post", "reaction", "bot", "boost"]:
            cnt = session.query(Task).filter(Task.type == t, Task.is_active == True, Task.is_paused == False,
                                             Task.expires_at > datetime.now()).count()
            counts[t] = cnt
        session.close()
        text = (
            f"👨‍💻 *Заработать*\n\n"
            f"📢 Каналы: {counts.get('channel',0)}\n"
            f"👥 Группы: {counts.get('group',0)}\n"
            f"👁 Просмотры: {counts.get('post',0)}\n"
            f"🤖 Боты: {counts.get('bot',0)}\n"
            f"⚡ Бусты: {counts.get('boost',0)}\n"
            f"🔥 Реакции: {counts.get('reaction',0)}\n\n"
            f"🔔 Выберите способ заработка 👇"
        )
        keyboard = [
            [InlineKeyboardButton("📺 Каналы", callback_data="earn_channels")],
            [InlineKeyboardButton("👥 Группы", callback_data="earn_groups")],
            [InlineKeyboardButton("👁 Просмотры", callback_data="earn_views")],
            [InlineKeyboardButton("🔥 Реакции", callback_data="earn_reactions")],
            [InlineKeyboardButton("🤖 Боты", callback_data="earn_bots")],
            [InlineKeyboardButton("⚡ Премиум буст", callback_data="earn_boost")],
            [InlineKeyboardButton("🔙 Назад", callback_data="main_menu")]
        ]
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

    async def show_tasks_by_type(self, update: Update, context: ContextTypes.DEFAULT_TYPE, task_type: str):
        query = update.callback_query
        await query.answer()
        page = context.user_data.get(f"{task_type}_page", 0)
        session = Session()
        tasks = session.query(Task).filter(
            Task.type == task_type,
            Task.is_active == True,
            Task.is_paused == False,
            Task.expires_at > datetime.now(),
            Task.current_completions < Task.max_completions
        ).order_by(Task.reward.desc()).all()
        total_pages = (len(tasks) + MAX_TASKS_PER_PAGE - 1) // MAX_TASKS_PER_PAGE
        start = page * MAX_TASKS_PER_PAGE
        tasks_page = tasks[start:start+MAX_TASKS_PER_PAGE]
        if not tasks_page:
            await query.edit_message_text("Нет заданий этого типа.", reply_markup=back_keyboard("earn"))
            session.close()
            return
        warning = ""
        if task_type in ("channel", "group", "bot"):
            warning = ("⚠️ *Запрещено отписываться ранее чем через 7 дней!*\n"
                       "Нарушители будут заблокированы, а заработанные средства аннулированы.\n\n")
        keyboard = []
        for task in tasks_page:
            if task_type in ("channel", "group", "bot"):
                url = task.extra_data if task.extra_data else f"https://t.me/{task.target_name}"
                keyboard.append([
                    InlineKeyboardButton(f"💰 {task.reward}{CURRENCY} | {task.target_name}", url=url),
                    InlineKeyboardButton("✅ Проверить", callback_data=f"verify_sub_{task.id}")
                ])
            elif task_type == "post":
                keyboard.append([InlineKeyboardButton(f"👁 {task.target_name} | {task.reward}{CURRENCY}",
                                                     callback_data=f"do_view_{task.id}")])
            elif task_type == "reaction":
                keyboard.append([InlineKeyboardButton(f"🔥 {task.target_name} | {task.reward}{CURRENCY}",
                                                     callback_data=f"do_reaction_{task.id}")])
            elif task_type == "boost":
                keyboard.append([InlineKeyboardButton(f"⚡ {task.target_name} | {task.reward}{CURRENCY}",
                                                     callback_data=f"do_boost_{task.id}")])
        # пагинация
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️", callback_data=f"tasks_{task_type}_prev"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("▶️", callback_data=f"tasks_{task_type}_next"))
        if nav:
            keyboard.append(nav)
        keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="earn")])
        await query.edit_message_text(warning, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        session.close()

    async def paginate_tasks(self, update: Update, context: ContextTypes.DEFAULT_TYPE, task_type: str, direction: str):
        query = update.callback_query
        await query.answer()
        key = f"{task_type}_page"
        page = context.user_data.get(key, 0)
        if direction == "next":
            page += 1
        else:
            page = max(0, page - 1)
        context.user_data[key] = page
        await self.show_tasks_by_type(update, context, task_type)

    # ----------------------------- ПРОВЕРКА ПОДПИСОК И ВЫПОЛНЕНИЕ -----------------------------
    async def verify_subscription(self, update: Update, context: ContextTypes.DEFAULT_TYPE, task_id: int):
        query = update.callback_query
        user_id = query.from_user.id
        session = Session()
        task = session.query(Task).filter_by(id=task_id, is_active=True).first()
        if not task or task.expires_at < datetime.now():
            await query.answer("Задание неактивно", show_alert=True)
            session.close()
            return
        existing = session.query(TaskCompletion).filter_by(task_id=task_id, user_id=user_id).first()
        if existing:
            await query.answer("Вы уже выполняли это задание", show_alert=True)
            session.close()
            return
        try:
            if task.type in ("channel", "group"):
                member = await context.bot.get_chat_member(chat_id=int(task.target_id), user_id=user_id)
                if member.status not in ("member", "administrator", "creator"):
                    await query.answer("❌ Вы не подписаны!", show_alert=True)
                    session.close()
                    return
            elif task.type == "bot":
                # бот не проверяем, считаем что пользователь нажал кнопку
                pass
        except BadRequest:
            await query.answer("Не удалось проверить подписку", show_alert=True)
            session.close()
            return
        # начисляем награду
        reward = task.reward
        update_balance(user_id, reward, is_earned=True)
        add_transaction(user_id, reward, "task_reward", f"Выполнение {task.type} {task.target_name}")
        completion = TaskCompletion(task_id=task_id, user_id=user_id, is_verified=True)
        session.add(completion)
        task.current_completions += 1
        if task.type in ("channel", "group"):
            sub = Subscription(user_id=user_id, channel_id=str(task.target_id), task_id=task_id,
                               check_until=datetime.now() + timedelta(days=UNSUBSCRIBE_BAN_DAYS))
            session.add(sub)
        session.commit()
        add_xp(user_id, XP_PER_TASK)
        await query.edit_message_text(
            f"✅ Задание выполнено! +{reward} {CURRENCY}\n"
            f"⚠️ Не отписывайтесь минимум 7 дней."
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
            await query.answer("Уже выполняли", show_alert=True)
            session.close()
            return
        context.user_data[f"pending_view_{user_id}"] = task_id
        await query.edit_message_text(
            f"👁 *Просмотр*\n\n"
            f"Перейдите по ссылке и посмотрите:\n{task.extra_data}\n\n"
            f"После просмотра отправьте скриншот в этот чат.\n"
            f"Награда: {task.reward}{CURRENCY}",
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
            await query.answer("Уже выполняли", show_alert=True)
            session.close()
            return
        context.user_data[f"pending_reaction_{user_id}"] = task_id
        await query.edit_message_text(
            f"🔥 *Реакция*\n\n"
            f"Поставьте реакцию под постом:\n{task.extra_data}\n\n"
            f"Отправьте скриншот с реакцией.\n"
            f"Награда: {task.reward}{CURRENCY}",
            parse_mode="Markdown",
            reply_markup=back_keyboard("earn")
        )
        session.close()

    async def handle_boost_task(self, update: Update, context: ContextTypes.DEFAULT_TYPE, task_id: int):
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
            f"⚡ *Буст*\n\n"
            f"Сделайте премиум-буст канала: https://t.me/{task.target_name}\n\n"
            f"Отправьте скриншот подтверждения.\n"
            f"Награда: {task.reward}{CURRENCY}",
            parse_mode="Markdown",
            reply_markup=back_keyboard("earn")
        )
        session.close()

    # ----------------------------- ПРИЁМ СКРИНШОТОВ -----------------------------
    async def handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        task_id = None
        for key in list(context.user_data.keys()):
            if key.startswith("pending_") and key.endswith(str(user_id)):
                task_id = context.user_data[key]
                del context.user_data[key]
                break
        if not task_id:
            await update.message.reply_text("Я не ожидал скриншот. Начните задание заново.")
            return
        session = Session()
        task = session.query(Task).filter_by(id=task_id).first()
        if not task:
            await update.message.reply_text("Задание не найдено.")
            session.close()
            return
        completion = TaskCompletion(task_id=task_id, user_id=user_id, is_verified=False,
                                    screenshot_message_id=update.message.message_id)
        session.add(completion)
        session.commit()
        # уведомляем владельца задания
        try:
            await context.bot.send_photo(
                chat_id=task.creator_id,
                photo=update.message.photo[-1].file_id,
                caption=f"📸 Новое выполнение задания #{task.id}\nОт: @{update.effective_user.username or user_id}\nНаграда: {task.reward}{CURRENCY}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Одобрить", callback_data=f"approve_{completion.id}"),
                     InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{completion.id}")]
                ])
            )
            await update.message.reply_text("✅ Скриншот отправлен на проверку. Ожидайте одобрения.")
        except Exception:
            await update.message.reply_text("Ошибка при отправке владельцу. Попробуйте позже.")
        session.close()

    async def approve_completion(self, update: Update, context: ContextTypes.DEFAULT_TYPE, completion_id: int):
        query = update.callback_query
        await query.answer()
        session = Session()
        comp = session.query(TaskCompletion).filter_by(id=completion_id).first()
        if not comp or comp.is_verified:
            await query.edit_message_text("Уже обработано")
            session.close()
            return
        task = session.query(Task).filter_by(id=comp.task_id).first()
        if not task:
            await query.edit_message_text("Задание не найдено")
            session.close()
            return
        reward = task.reward
        update_balance(comp.user_id, reward, is_earned=True)
        add_transaction(comp.user_id, reward, "task_reward", f"Одобрено {task.type}")
        comp.is_verified = True
        comp.approved_at = datetime.now()
        task.current_completions += 1
        if task.type in ("channel", "group"):
            sub = Subscription(user_id=comp.user_id, channel_id=task.target_id, task_id=task.id,
                               check_until=datetime.now() + timedelta(days=UNSUBSCRIBE_BAN_DAYS))
            session.add(sub)
        session.commit()
        add_xp(comp.user_id, XP_PER_TASK)
        await query.edit_message_text(f"✅ Задание одобрено. Пользователь получил {reward}{CURRENCY}.")
        try:
            await context.bot.send_message(comp.user_id, f"✅ Ваше задание #{task.id} одобрено! +{reward}{CURRENCY}")
        except:
            pass
        session.close()

    async def reject_completion(self, update: Update, context: ContextTypes.DEFAULT_TYPE, completion_id: int):
        query = update.callback_query
        await query.answer()
        session = Session()
        comp = session.query(TaskCompletion).filter_by(id=completion_id).first()
        if not comp or comp.is_verified:
            await query.edit_message_text("Уже обработано")
            session.close()
            return
        session.delete(comp)
        session.commit()
        await query.edit_message_text("❌ Выполнение отклонено. Пользователь может повторить.")
        try:
            await context.bot.send_message(comp.user_id, f"❌ Ваше задание отклонено. Попробуйте снова.")
        except:
            pass
        session.close()

    # ----------------------------- РЕКЛАМИРОВАТЬ (СОЗДАНИЕ ЗАДАНИЙ) -----------------------------
    async def advertise_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        user = get_user(query.from_user.id)
        text = f"📢 Что рекламировать?\n💰 Баланс: {user.balance} {CURRENCY}"
        keyboard = [
            [InlineKeyboardButton("📺 Канал", callback_data="ad_channel")],
            [InlineKeyboardButton("👥 Группа", callback_data="ad_group")],
            [InlineKeyboardButton("👁 Пост", callback_data="ad_post")],
            [InlineKeyboardButton("🤖 Бот", callback_data="ad_bot")],
            [InlineKeyboardButton("⚡ Премиум буст", callback_data="ad_boost")],
            [InlineKeyboardButton("🔥 Реакции", callback_data="ad_reaction")],
            [InlineKeyboardButton("🔙 Назад", callback_data="main_menu")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

    async def start_ad_creation(self, update: Update, context: ContextTypes.DEFAULT_TYPE, ad_type: str):
        query = update.callback_query
        await query.answer()
        context.user_data["ad_type"] = ad_type
        await query.edit_message_text("Введите ссылку на ресурс (или @username):", reply_markup=back_keyboard("advertise"))
        return "awaiting_target"

    async def receive_target(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        ad_type = context.user_data.get("ad_type")
        if not ad_type:
            await update.message.reply_text("Начните заново через Рекламировать.")
            return ConversationHandler.END
        target = update.message.text.strip()
        # обработка ссылки
        if ad_type in ("channel", "group", "bot", "boost"):
            # извлекаем username
            if target.startswith("https://t.me/"):
                username = target.split("https://t.me/")[-1].replace("/", "")
            elif target.startswith("@"):
                username = target[1:]
            else:
                username = target
            context.user_data["target_name"] = username
            context.user_data["target_id"] = username
            context.user_data["extra_data"] = f"https://t.me/{username}"
            if ad_type in ("channel", "group"):
                try:
                    chat = await context.bot.get_chat(username)
                    member = await context.bot.get_chat_member(chat.id, context.bot.id)
                    if member.status not in ("administrator", "creator"):
                        await update.message.reply_text("❌ Бот не администратор! Добавьте бота и выдайте права.")
                        return ConversationHandler.END
                    context.user_data["target_id"] = str(chat.id)
                except Exception:
                    await update.message.reply_text("Не удалось проверить права бота. Убедитесь, что бот админ.")
                    return ConversationHandler.END
        else:
            # для постов и реакций просто сохраняем ссылку
            context.user_data["target_name"] = target
            context.user_data["extra_data"] = target
        await update.message.reply_text("Введите награду за выполнение (число, 1–1000):")
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
        await update.message.reply_text("Введите максимальное количество выполнений (1–1000):")
        return "awaiting_max"

    async def receive_max(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            max_completions = int(update.message.text)
            if max_completions < 1 or max_completions > 1000:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Введите число от 1 до 1000.")
            return "awaiting_max"
        ad_type = context.user_data["ad_type"]
        reward = context.user_data["reward"]
        total = reward * max_completions
        user_id = update.effective_user.id
        user = get_user(user_id)
        commission = 0
        if user.balance - user.earned_balance < total:
            commission = int(total * COMMISSION_RATE)
        total_needed = total + commission
        if user.balance < total_needed:
            await update.message.reply_text(f"❌ Недостаточно средств. Нужно {total_needed} {CURRENCY}, у вас {user.balance}.")
            return ConversationHandler.END
        update_balance(user_id, -total_needed)
        add_transaction(user_id, -total_needed, "task_creation", f"Создание {ad_type} задания")
        session = Session()
        task = Task(
            type=ad_type,
            target_id=context.user_data.get("target_id", ""),
            target_name=context.user_data["target_name"],
            reward=reward,
            max_completions=max_completions,
            creator_id=user_id,
            expires_at=datetime.now() + timedelta(days=TASK_DURATION_DAYS),
            extra_data=context.user_data["extra_data"]
        )
        session.add(task)
        session.commit()
        session.close()
        await update.message.reply_text(
            f"✅ Задание создано!\n"
            f"Тип: {ad_type}, Цель: {context.user_data['target_name']}\n"
            f"Награда: {reward}{CURRENCY}, Макс: {max_completions}\n"
            f"Списано: {total_needed}{CURRENCY} (комиссия {commission}{CURRENCY})"
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
            "💸 *Чеки*\n\n"
            "• Персональный – перевод одному пользователю\n"
            "• Мульти-чек – перевод нескольким (можно с условием подписки)",
            parse_mode="Markdown",
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
        await query.edit_message_text("Введите сумму для каждого получателя:", reply_markup=back_keyboard("checks_menu"))
        return "await_check_amount"

    async def receive_check_amount(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            amount = int(update.message.text)
            if amount < 1:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Введите положительное число.")
            return "await_check_amount"
        context.user_data["check_amount"] = amount
        check_type = context.user_data["check_type"]
        if check_type == "personal":
            await update.message.reply_text("Введите ID получателя (число):")
            return "await_personal_recipient"
        else:
            await update.message.reply_text("Введите количество получателей (макс 100):")
            return "await_multi_count"

    async def receive_personal_recipient(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            recipient = int(update.message.text)
        except ValueError:
            await update.message.reply_text("ID должен быть числом.")
            return "await_personal_recipient"
        amount = context.user_data["check_amount"]
        user_id = update.effective_user.id
        session = Session()
        user = session.query(User).filter_by(user_id=user_id).first()
        if user.balance < amount:
            await update.message.reply_text(f"Недостаточно средств. Баланс: {user.balance}")
            session.close()
            return ConversationHandler.END
        code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=10))
        check = Check(
            type="personal", owner_id=user_id, amount=amount, total_amount=amount,
            remaining=amount, max_uses=1, code=code
        )
        session.add(check)
        user.balance -= amount
        add_transaction(user_id, -amount, "check_creation", f"Персональный чек на {amount}")
        session.commit()
        session.close()
        await update.message.reply_text(
            f"✅ Чек создан!\nСумма: {amount}{CURRENCY}\nКод: `{code}`\nПолучатель: /claim {code}",
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
        await update.message.reply_text("Введите ссылку на канал (условие подписки) или 0, если без условия:")
        return "await_multi_channel"

    async def receive_multi_channel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        channel_input = update.message.text.strip()
        required_channel = None
        if channel_input != "0":
            # извлекаем username
            if channel_input.startswith("https://t.me/"):
                username = channel_input.split("https://t.me/")[-1].replace("/", "")
            elif channel_input.startswith("@"):
                username = channel_input[1:]
            else:
                username = channel_input
            try:
                chat = await context.bot.get_chat(username)
                member = await context.bot.get_chat_member(chat.id, context.bot.id)
                if member.status not in ("administrator", "creator"):
                    await update.message.reply_text("❌ Бот не администратор канала. Добавьте бота или введите 0.")
                    return "await_multi_channel"
                required_channel = str(chat.id)
            except Exception:
                await update.message.reply_text("Не удалось проверить канал. Введите 0 или корректную ссылку.")
                return "await_multi_channel"
        amount = context.user_data["check_amount"]
        count = context.user_data["multi_count"]
        total = amount * count
        user_id = update.effective_user.id
        session = Session()
        user = session.query(User).filter_by(user_id=user_id).first()
        if user.balance < total:
            await update.message.reply_text(f"Недостаточно средств. Нужно {total}{CURRENCY}")
            session.close()
            return ConversationHandler.END
        code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=10))
        check = Check(
            type="multi", owner_id=user_id, amount=amount, total_amount=total,
            remaining=total, max_uses=count, required_channel=required_channel, code=code
        )
        session.add(check)
        user.balance -= total
        add_transaction(user_id, -total, "check_creation", f"Мульти-чек на {count} чел.")
        session.commit()
        session.close()
        await update.message.reply_text(
            f"✅ Мульти-чек создан!\n"
            f"Сумма на каждого: {amount}{CURRENCY}\n"
            f"Кол-во: {count}\n"
            f"Условие: {'подписка на канал' if required_channel else 'нет'}\n"
            f"Код: `{code}`\n"
            f"Активация: /claim {code}",
            parse_mode="Markdown"
        )
        context.user_data.clear()
        return ConversationHandler.END

    async def claim_check(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        args = context.args
        if not args:
            await update.message.reply_text("Использование: /claim <код>")
            return
        code = args[0]
        user_id = update.effective_user.id
        session = Session()
        check = session.query(Check).filter_by(code=code, is_active=True).first()
        if not check:
            await update.message.reply_text("Чек не найден или неактивен.")
            session.close()
            return
        if check.used_count >= check.max_uses:
            await update.message.reply_text("Чек уже полностью использован.")
            session.close()
            return
        if check.required_channel:
            try:
                member = await context.bot.get_chat_member(int(check.required_channel), user_id)
                if member.status not in ("member", "administrator", "creator"):
                    await update.message.reply_text("❌ Вы не подписаны на требуемый канал.")
                    session.close()
                    return
            except:
                await update.message.reply_text("❌ Не удалось проверить подписку.")
                session.close()
                return
        user = session.query(User).filter_by(user_id=user_id).first()
        if not user:
            user = User(user_id=user_id, referral_code=''.join(random.choices(string.ascii_uppercase+string.digits,k=8)))
            session.add(user)
            session.flush()
        user.balance += check.amount
        add_transaction(user_id, check.amount, "check_reward", f"Активация чека {code}")
        check.used_count += 1
        check.remaining -= check.amount
        if check.used_count >= check.max_uses:
            check.is_active = False
        session.commit()
        await update.message.reply_text(f"✅ Вы получили {check.amount} {CURRENCY} по чеку {code}!")
        session.close()

    # ----------------------------- МОЙ КАБИНЕТ -----------------------------
    async def cabinet(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        user_id = query.from_user.id
        user = get_user(user_id)
        session = Session()
        total_earned = session.query(func.sum(Transaction.amount)).filter(Transaction.user_id==user_id, Transaction.type=="task_reward").scalar() or 0
        total_spent = session.query(func.sum(Transaction.amount)).filter(Transaction.user_id==user_id, Transaction.type=="task_creation").scalar() or 0
        level_xp_needed = user.level * XP_LEVEL_BASE
        text = (
            f"👤 *Личный кабинет*\n\n"
            f"ID: `{user_id}`\n"
            f"⭐ Уровень: {user.level} (XP: {user.xp}/{level_xp_needed})\n"
            f"💰 Баланс: {user.balance} {CURRENCY}\n"
            f"📈 Заработано: {total_earned} {CURRENCY}\n"
            f"📉 Потрачено: {total_spent} {CURRENCY}\n\n"
            f"📎 Реферальная ссылка:\nhttps://t.me/{context.bot.username}?start=ref_{user.referral_code}"
        )
        keyboard = [
            [InlineKeyboardButton("📈 Пополнить", callback_data="deposit")],
            [InlineKeyboardButton("💬 Рефералы", callback_data="referral_info")],
            [InlineKeyboardButton("✅ Уровни", callback_data="level_info")],
            [InlineKeyboardButton("🟢 Мои задания", callback_data="my_tasks")],
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
            status = "🟢 Активно" if (t.is_active and t.expires_at > datetime.now()) else "🔴 Завершено"
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
        task = session.query(Task).filter_by(id=task_id, creator_id=user_id, is_active=True).first()
        if not task:
            await query.answer("Задание не найдено")
            session.close()
            return
        remaining = task.max_completions - task.current_completions
        refund = task.reward * remaining
        update_balance(user_id, refund)
        add_transaction(user_id, refund, "refund", f"Удаление задания {task.id}")
        task.is_active = False
        session.commit()
        await query.edit_message_text(f"Задание удалено. Возвращено {refund}{CURRENCY}.")
        session.close()

    # ----------------------------- ФОНОВЫЕ ЗАДАЧИ -----------------------------
    async def check_unsubscribes(self):
        if not self.application:
            return
        session = Session()
        now = datetime.now()
        subs = session.query(Subscription).filter(Subscription.check_until > now).all()
        for sub in subs:
            try:
                member = await self.application.bot.get_chat_member(int(sub.channel_id), sub.user_id)
                if member.status not in ("member", "administrator", "creator"):
                    user = session.query(User).filter_by(user_id=sub.user_id).first()
                    if user and not user.is_banned:
                        user.is_banned = True
                        user.ban_until = now + timedelta(days=UNSUBSCRIBE_BAN_DAYS)
                        session.commit()
                        await self.application.bot.send_message(sub.user_id, f"Вы заблокированы за отписку ранее 7 дней. Разблокировка {user.ban_until.strftime('%d.%m.%Y')}")
            except:
                pass
        session.close()

    async def auto_approve_tasks(self):
        if not self.application:
            return
        session = Session()
        timeout = datetime.now() - timedelta(hours=AUTO_APPROVE_HOURS)
        completions = session.query(TaskCompletion).filter(
            TaskCompletion.is_verified == False,
            TaskCompletion.completed_at < timeout
        ).all()
        for comp in completions:
            task = session.query(Task).filter_by(id=comp.task_id).first()
            if task and task.is_active:
                update_balance(comp.user_id, task.reward, is_earned=True)
                add_transaction(comp.user_id, task.reward, "task_reward", f"Автоодобрение задания {task.id}")
                comp.is_verified = True
                comp.approved_at = datetime.now()
                task.current_completions += 1
                if task.type in ("channel", "group"):
                    sub = Subscription(user_id=comp.user_id, channel_id=task.target_id, task_id=task.id,
                                       check_until=datetime.now() + timedelta(days=UNSUBSCRIBE_BAN_DAYS))
                    session.add(sub)
                session.commit()
                await self.application.bot.send_message(comp.user_id, f"✅ Ваше задание #{task.id} автоматически одобрено! +{task.reward}{CURRENCY}")
        session.close()

    async def clean_expired_tasks(self):
        if not self.application:
            return
        session = Session()
        expired = session.query(Task).filter(Task.expires_at < datetime.now(), Task.is_active == True).all()
        for task in expired:
            task.is_active = False
        session.commit()
        session.close()

    # ----------------------------- ЗАПУСК -----------------------------
    async def set_commands(self, application: Application):
        commands = [
            BotCommand("start", "Главное меню"),
            BotCommand("help", "Помощь"),
            BotCommand("stats", "Статистика"),
            BotCommand("rules", "Правила"),
            BotCommand("support", "Поддержка"),
            BotCommand("claim", "Активировать чек")
        ]
        await application.bot.set_my_commands(commands)

    def run(self):
        application = Application.builder().token(BOT_TOKEN).build()
        self.application = application

        # Регистрация команд
        application.add_handler(CommandHandler("start", self.start))
        application.add_handler(CommandHandler("help", self.help_command))
        application.add_handler(CommandHandler("stats", self.stats_command))
        application.add_handler(CommandHandler("rules", self.rules_command))
        application.add_handler(CommandHandler("support", self.support_command))
        application.add_handler(CommandHandler("claim", self.claim_check))

        # ConversationHandler для создания заданий
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
        application.add_handler(ad_conv)

        # ConversationHandler для чеков
        check_conv = ConversationHandler(
            entry_points=[
                CallbackQueryHandler(self.create_personal_check, pattern="^create_personal_check$"),
                CallbackQueryHandler(self.create_multi_check, pattern="^create_multi_check$")
            ],
            states={
                "await_check_amount": [MessageHandler(filters.TEXT & ~filters.COMMAND, self.receive_check_amount)],
                "await_personal_recipient": [MessageHandler(filters.TEXT & ~filters.COMMAND, self.receive_personal_recipient)],
                "await_multi_count": [MessageHandler(filters.TEXT & ~filters.COMMAND, self.receive_multi_count)],
                "await_multi_channel": [MessageHandler(filters.TEXT & ~filters.COMMAND, self.receive_multi_channel)]
            },
            fallbacks=[],
            per_message=False
        )
        application.add_handler(check_conv)

        # Обработчики кнопок
        application.add_handler(CallbackQueryHandler(self.main_menu, pattern="^main_menu$"))
        application.add_handler(CallbackQueryHandler(self.earn_menu, pattern="^earn$"))
        application.add_handler(CallbackQueryHandler(self.advertise_menu, pattern="^advertise$"))
        application.add_handler(CallbackQueryHandler(self.checks_menu, pattern="^checks_menu$"))
        application.add_handler(CallbackQueryHandler(self.cabinet, pattern="^cabinet$"))
        application.add_handler(CallbackQueryHandler(self.my_tasks, pattern="^my_tasks$"))
        application.add_handler(CallbackQueryHandler(self.delete_task, pattern="^delete_task_"))

        # Обработчики выбора типа заданий
        for typ in ["channels", "groups", "views", "reactions", "bots", "boost"]:
            application.add_handler(CallbackQueryHandler(lambda u,c: self.show_tasks_by_type(u,c,typ.replace("views","post").replace("reactions","reaction").replace("bots","bot").replace("channels","channel").replace("groups","group")), pattern=f"^earn_{typ}$"))

        # Пагинация
        for typ in ["channel","group","post","reaction","bot","boost"]:
            application.add_handler(CallbackQueryHandler(lambda u,c: self.paginate_tasks(u,c,typ,"next"), pattern=f"^tasks_{typ}_next$"))
            application.add_handler(CallbackQueryHandler(lambda u,c: self.paginate_tasks(u,c,typ,"prev"), pattern=f"^tasks_{typ}_prev$"))

        # Проверка подписки и выполнение
        application.add_handler(CallbackQueryHandler(lambda u,c: self.verify_subscription(u,c,int(c.data.split("_")[2])), pattern="^verify_sub_"))
        application.add_handler(CallbackQueryHandler(lambda u,c: self.handle_view_task(u,c,int(c.data.split("_")[2])), pattern="^do_view_"))
        application.add_handler(CallbackQueryHandler(lambda u,c: self.handle_reaction_task(u,c,int(c.data.split("_")[2])), pattern="^do_reaction_"))
        application.add_handler(CallbackQueryHandler(lambda u,c: self.handle_boost_task(u,c,int(c.data.split("_")[2])), pattern="^do_boost_"))

        # Одобрение/отклонение
        application.add_handler(CallbackQueryHandler(lambda u,c: self.approve_completion(u,c,int(c.data.split("_")[1])), pattern="^approve_"))
        application.add_handler(CallbackQueryHandler(lambda u,c: self.reject_completion(u,c,int(c.data.split("_")[1])), pattern="^reject_"))

        # Прочие кнопки
        application.add_handler(CallbackQueryHandler(lambda u,c: self.cabinet(u,c), pattern="^my_tasks$"))
        application.add_handler(CallbackQueryHandler(lambda u,c: self.main_menu(u,c), pattern="^deposit$"))
        application.add_handler(CallbackQueryHandler(lambda u,c: self.main_menu(u,c), pattern="^referral_info$"))
        application.add_handler(CallbackQueryHandler(lambda u,c: self.main_menu(u,c), pattern="^level_info$"))

        # Приём фото
        application.add_handler(MessageHandler(filters.PHOTO, self.handle_photo))

        # Фоновые задачи
        self.scheduler.add_job(self.check_unsubscribes, 'interval', hours=6)
        self.scheduler.add_job(self.auto_approve_tasks, 'interval', hours=1)
        self.scheduler.add_job(self.clean_expired_tasks, 'interval', hours=12)
        self.scheduler.start()

        application.post_init = self.set_commands

        logging.basicConfig(level=logging.INFO)
        logger = logging.getLogger(__name__)
        logger.info("Бот запущен")
        application.run_polling()

if __name__ == "__main__":
    PromoBot().run()
