import discord
from discord.ext import commands, tasks
import os
import base64
import json
import logging
import time
import asyncio
import aiohttp # requests 대신 사용할 비동기 HTTP 라이브러리
import pytz
from datetime import datetime, timedelta
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, firestore
from dateutil import parser

# --- 1. 기본 설정 ---
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] [%(levelname)s] %(message)s')

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
REPORT_CHANNEL_ID = int(os.getenv("REPORT_CHANNEL_ID"))
firebase_key_base64 = os.getenv("FIREBASE_KEY_BASE64")

if not all([DISCORD_TOKEN, GITHUB_TOKEN, firebase_key_base64]):
    raise ValueError("❌ DISCORD_TOKEN, GITHUB_TOKEN, FIREBASE_KEY_BASE64 환경변수가 필요합니다!")

# Firebase 초기화
cred_dict = json.loads(base64.b64decode(firebase_key_base64).decode("utf-8"))
cred = credentials.Certificate(cred_dict)
firebase_admin.initialize_app(cred)
db = firestore.client()

# 봇 인텐트 설정
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

KST = pytz.timezone("Asia/Seoul")

# --- 2. 비동기 도우미 함수 (I/O 작업을 멈추지 않게 함) ---

# Firestore 작업을 비동기로 처리
def _db_get(ref): return ref.get()
async def db_get(ref): return await bot.loop.run_in_executor(None, _db_get, ref)

def _db_set(ref, data): ref.set(data)
async def db_set(ref, data): await bot.loop.run_in_executor(None, _db_set, ref, data)

def _db_update(ref, data): ref.update(data)
async def db_update(ref, data): await bot.loop.run_in_executor(None, _db_update, ref, data)

def _db_delete(ref): ref.delete()
async def db_delete(ref): await bot.loop.run_in_executor(None, _db_delete, ref)

def _db_stream(collection_ref): return list(collection_ref.stream())
async def db_stream(collection_ref): return await bot.loop.run_in_executor(None, _db_stream, collection_ref)

# aiohttp를 사용한 비동기 GitHub API 호출
async def fetch_github_api(session, url):
    headers = {"Accept": "application/vnd.github.v3+json", "Authorization": f"Bearer {GITHUB_TOKEN}"}
    async with session.get(url, headers=headers) as response:
        logging.info(f"📡 GitHub API 요청 → URL: {url}, 상태: {response.status}")
        if response.status == 200:
            return await response.json()
        text = await response.text()
        logging.warning(f"❌ GitHub API 호출 실패 (상태: {response.status})\n응답: {text}")
        return None

async def get_valid_commits(session, user_data, now_kst):
    github_id = user_data.get("github_id")
    repo_name = user_data.get("repo_name")
    start_of_day_kst = now_kst.replace(hour=0, minute=0, second=0, microsecond=0)
    since_utc = start_of_day_kst.astimezone(pytz.utc).isoformat()
    
    url = f"https://api.github.com/repos/{github_id}/{repo_name}/commits?since={since_utc}"
    all_commits = await fetch_github_api(session, url)
    if all_commits is None: return 0

    valid_count = 0
    for c in all_commits:
        try:
            commit_time_utc = parser.isoparse(c['commit']['committer']['date'])
            commit_time_kst = commit_time_utc.astimezone(KST)
            if commit_time_kst.date() == now_kst.date():
                valid_count += 1
        except (KeyError, TypeError):
            continue
    return valid_count

# --- 3. 디스코드 명령어 (전체 비동기화 및 개선) ---

@bot.command(name="등록")
@commands.has_permissions(administrator=True)
async def register_user(ctx, member: discord.Member, github_id: str, repo_name: str, goal_per_day: int):
    async with ctx.typing():
        repo_url = f"https://api.github.com/repos/{github_id}/{repo_name}"
        if not await fetch_github_api(bot.http_session, repo_url):
            await ctx.send("❌ 존재하지 않는 GitHub 레포지토리입니다. 사용자 ID와 레포지토리 이름을 확인해주세요.")
            return

        user_ref = db.collection("users").document(str(member.id))
        if (await db_get(user_ref)).exists:
            await ctx.send(f"⚠️ {member.mention}님은 이미 등록된 사용자입니다.")
            return

        user_data = {
            "github_id": github_id, "repo_name": repo_name, "goal_per_day": goal_per_day,
            "history": {}, "weekly_fail": 0, "total_fail": 0, "on_vacation": False
        }
        await db_set(user_ref, user_data)
        await ctx.send(f"✅ {member.mention} 등록 완료: `{github_id}/{repo_name}`, 목표: **{goal_per_day}회/일**")

@bot.command(name="인증")
async def certify_commit(ctx):
    async with ctx.typing():
        user_ref = db.collection("users").document(str(ctx.author.id))
        user_doc = await db_get(user_ref)
        if not user_doc.exists:
            await ctx.send("❌ 먼저 `!등록` 명령어로 등록해주세요.")
            return
        user_data = user_doc.to_dict()

        now_kst = datetime.now(KST)
        if now_kst.weekday() >= 5:
            await ctx.send("🌴 주말인디 살살하세요 행님 ☕")
            return
        if user_data.get("on_vacation", False):
            await ctx.send("🏝️ 휴가 가서도 코테? 에밥니다 헴")
            return

        commits = await get_valid_commits(bot.http_session, user_data, now_kst)
        passed = commits >= user_data.get("goal_per_day", 1)
        
        date_str = now_kst.strftime("%Y-%m-%d")
        await db_update(user_ref, {f"history.{date_str}": {"commits": commits, "passed": passed}})

        result_msg = "✅ 통과! 🎉" if passed else "❌ 커피 한 잔 할래요옹~ 😢"
        embed = discord.Embed(
            title=f"{ctx.author.display_name}님 인증 결과",
            description=f"**{result_msg}**",
            color=discord.Color.green() if passed else discord.Color.red()
        )
        embed.add_field(name="GitHub", value=f"`{user_data['github_id']}`", inline=True)
        embed.add_field(name="오늘 커밋 / 목표", value=f"**{commits}** / {user_data['goal_per_day']}", inline=True)
        await ctx.send(embed=embed)

@bot.command(name="유저목록")
async def user_list(ctx):
    async with ctx.typing():
        users_stream = await db_stream(db.collection("users"))
        lines = []
        for i, user_snapshot in enumerate(users_stream):
            doc = user_snapshot.to_dict()
            status = "🏝️ 휴가중" if doc.get("on_vacation") else "✅ 활동중"
            lines.append(f"{i+1}. <@{user_snapshot.id}> (`{doc.get('github_id')}`) - {status}")

        if not lines:
            await ctx.send("등록된 유저가 없습니다.")
            return
        
        embed = discord.Embed(title="📋 등록된 유저 목록", description="\n".join(lines), color=discord.Color.blue())
        await ctx.send(embed=embed)

@bot.command(name="삭제")
@commands.has_permissions(administrator=True)
async def delete_user(ctx, member: discord.Member):
    async with ctx.typing():
        user_ref = db.collection("users").document(str(member.id))
        if not (await db_get(user_ref)).exists:
            await ctx.send("❌ 해당 유저는 등록되어 있지 않습니다.")
            return
        await db_delete(user_ref)
        await ctx.send(f"🗑️ {member.mention} 유저 정보를 삭제했습니다.")

@bot.command(name="수정")
@commands.has_permissions(administrator=True)
async def edit_user(ctx, member: discord.Member, key: str, *, value: str):
    async with ctx.typing():
        valid_keys = {"github_id", "repo_name", "goal_per_day"}
        if key not in valid_keys:
            await ctx.send(f"❌ 수정할 수 없는 항목입니다. (`{', '.join(valid_keys)}` 중 하나여야 합니다.)")
            return
        
        user_ref = db.collection("users").document(str(member.id))
        if not (await db_get(user_ref)).exists:
            await ctx.send("❌ 해당 유저는 등록되어 있지 않습니다.")
            return

        update_data = {key: int(value) if key == "goal_per_day" else value}
        await db_update(user_ref, update_data)
        await ctx.send(f"🔧 {member.mention}님의 `{key}` 정보를 `{value}`(으)로 수정했습니다.")

@bot.command(name="기각수정")
@commands.has_permissions(administrator=True)
async def edit_fails(ctx, member: discord.Member, amount: int):
    async with ctx.typing():
        user_ref = db.collection("users").document(str(member.id))
        user_doc = await db_get(user_ref)
        if not user_doc.exists:
            await ctx.send("❌ 해당 유저는 등록되어 있지 않습니다.")
            return
        
        # Firestore.Increment를 사용하여 안전하게 값을 변경
        await db_update(user_ref, {
            "total_fail": firestore.Increment(amount),
            "weekly_fail": firestore.Increment(amount)
        })
        new_total = user_doc.to_dict().get("total_fail", 0) + amount
        await ctx.send(f"🔧 {member.mention}님의 기각 횟수수수수퍼 노바")

@bot.command(name="커피왕")
async def coffee_king(ctx):
    async with ctx.typing():
        users_stream = await db_stream(db.collection("users"))
        ranking = [(s.id, s.to_dict().get("total_fail", 0)) for s in users_stream if s.to_dict().get("total_fail", 0) > 0]
        
        if not ranking:
            await ctx.send("☕ **커피왕 랭킹** ☕\n\n🥳 모두 0잔!? 커피왕이 아니라 코딩왕이셈요 행님덜!")
            return

        ranking.sort(key=lambda x: x[1], reverse=True)
        lines = [f"🏆 **{i+1}위**: <@{uid}> - 누적 **{score}**회" for i, (uid, score) in enumerate(ranking[:10])] # 상위 10명만 표시
        
        embed = discord.Embed(title="☕ 커피왕 랭킹 ☕", description="\n".join(lines), color=discord.Color.dark_gold())
        await ctx.send(embed=embed)

@bot.command(name="휴가")
@commands.has_permissions(administrator=True)
async def set_vacation(ctx, member: discord.Member):
    await db_update(db.collection("users").document(str(member.id)), {"on_vacation": True})
    await ctx.send(f"🏝️ {member.mention} 님을 휴가 상태로 전환했습니다.")

@bot.command(name="복귀")
@commands.has_permissions(administrator=True)
async def unset_vacation(ctx, member: discord.Member):
    await db_update(db.collection("users").document(str(member.id)), {"on_vacation": False})
    await ctx.send(f"👋 {member.mention} 님이 복귀했습니다!")

# --- 4. 백그라운드 작업 (Tasks) ---

@tasks.loop(hours=1)
async def daily_check():
    await bot.wait_until_ready()
    now = datetime.now(KST)
    
    # 매일 밤 11시 59분에만 작동
    if now.weekday() >= 5 or now.hour != 23 or now.minute != 59:
        return

    logging.info(f"--- 🌙 {now.strftime('%Y-%m-%d')} 일일 기각자 체크 시작 ---")
    users_stream = await db_stream(db.collection("users"))
    channel = bot.get_channel(REPORT_CHANNEL_ID)
    failed_users = []

    for user_snapshot in users_stream:
        user_id = user_snapshot.id
        doc = user_snapshot.to_dict()
        if doc.get("on_vacation", False): continue

        history = doc.get("history", {})
        today_data = history.get(now.strftime("%Y-%m-%d"))
        
        if not today_data or not today_data.get("passed", False):
            failed_users.append(user_id)
            await db_update(db.collection("users").document(user_id), {
                "weekly_fail": firestore.Increment(1),
                "total_fail": firestore.Increment(1)
            })

    if failed_users:
        mentions = " ".join([f"<@{uid}>" for uid in failed_users])
        await channel.send(f"📢 **[{now.strftime('%Y-%m-%d')}] 기각자 목록:**\n{mentions}")
    else:
        await channel.send(f"🎉 **[{now.strftime('%Y-%m-%d')}] 전원 통과!** 굿보이 굿걸! 👏")

@tasks.loop(hours=1)
async def weekly_reset():
    await bot.wait_until_ready()
    now = datetime.now(KST)
    
    # 매주 목요일 자정에만 작동 (수요일 -> 목요일 넘어가는 자정)
    if now.weekday() != 3 or now.hour != 0 or now.minute != 0:
        return
        
    logging.info("--- ☕ 주간 커피왕 발표 및 초기화 시작 ---")
    users_stream = await db_stream(db.collection("users"))
    channel = bot.get_channel(REPORT_CHANNEL_ID)
    
    weekly_fails = {s.id: s.to_dict().get("weekly_fail", 0) for s in users_stream}
    max_fail = max(weekly_fails.values()) if weekly_fails else 0
    
    if max_fail > 0:
        kings = [uid for uid, fails in weekly_fails.items() if fails == max_fail]
        mentions = " ".join([f"<@{uid}>" for uid in kings])
        await channel.send(f"🥶 **이번 주 커피 당첨자 (기각 {max_fail}회):**\n{mentions} !! 음 달다 달아~")
    else:
        await channel.send("🎉 **이번 주는 커피왕 없음!** 모두 수고하셨습니다!")

    # 주간 실패 횟수 초기화
    for user_id in weekly_fails.keys():
        await db_update(db.collection("users").document(user_id), {"weekly_fail": 0})
    logging.info("--- 📅 주간 실패 횟수 초기화 완료 ---")


# --- 5. 이벤트 핸들러 및 봇 실행 ---

@bot.event
async def on_ready():
    bot.http_session = aiohttp.ClientSession()
    logging.info(f"✅ 봇 로그인 완료: {bot.user}")
    daily_check.start()
    weekly_reset.start()

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"😅 명령어를 너무 자주 사용했어요. **{int(error.retry_after) + 1}초** 뒤에 다시 시도해주세요.", delete_after=5)
    elif isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
        await ctx.send(f"🤔 인자가 잘못되었어요. `{ctx.prefix}{ctx.command.name} {ctx.command.signature}` 형식을 확인해주세요.")
    elif isinstance(error, commands.CheckFailure):
        await ctx.send("🚫 이 명령어를 사용할 권한이 없습니다.")
    else:
        logging.exception(f"명령어 '{ctx.command}' 처리 중 오류: {error}")
        await ctx.send("❌ 명령 처리 중 오류가 발생했습니다. 관리자에게 문의해주세요.")

async def main():
    async with bot:
        await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, RuntimeError):
        logging.info("봇을 종료합니다.")
    finally:
        # 프로그램 종료 시 aiohttp 세션을 안전하게 닫음
        if bot.is_ready() and hasattr(bot, 'http_session'):
            asyncio.run(bot.http_session.close())
            logging.info("📡 aiohttp 클라이언트 세션 종료됨")