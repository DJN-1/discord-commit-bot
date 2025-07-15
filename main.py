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
from datetime import datetime, timedelta, time
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

# --- 진행 중인 인증 작업을 추적하는 전역 변수 ---
certifying_users = set()

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
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as response:
            logging.info(f"📡 GitHub API 요청 → URL: {url}, 상태: {response.status}")
            if response.status == 200:
                return await response.json()
            text = await response.text()
            logging.warning(f"❌ GitHub API 호출 실패 (상태: {response.status})\n응답: {text}")
            return None
    except asyncio.TimeoutError:
        logging.error(f"⏰ GitHub API 호출 타임아웃: {url}")
        return None
    except Exception as e:
        logging.error(f"🔥 GitHub API 호출 중 예외 발생: {e}")
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
        try:
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
        except Exception as e:
            logging.error(f"등록 중 오류 발생: {e}")
            await ctx.send("❌ 등록 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.")

@bot.command(name="인증")
@commands.cooldown(1, 5, commands.BucketType.user)  # 유저당 5초 쿨다운
async def certify_commit(ctx):
    user_id = ctx.author.id
    
    # 이미 인증 중인 사용자인지 확인
    if user_id in certifying_users:
        await ctx.send("⏳ 이미 인증 처리 중입니다. 잠시만 기다려주세요.", delete_after=5)
        return
    
    # 인증 처리 중 상태로 설정
    certifying_users.add(user_id)
    
    try:
        async with ctx.typing():
            user_ref = db.collection("users").document(str(user_id))
            user_doc = await db_get(user_ref)
            if not user_doc.exists:
                await ctx.send("❌ 먼저 `!등록` 명령어로 등록해주세요.")
                return
            user_data = user_doc.to_dict()

            now_kst = datetime.now(KST)
            date_str = now_kst.strftime("%Y-%m-%d")
            
            # 이미 오늘 인증한 기록이 있는지 확인
            history = user_data.get("history", {})
            if date_str in history:
                today_record = history[date_str]
                passed = today_record.get("passed", False)
                commits = today_record.get("commits", 0)
                
                status_msg = "✅ 통과" if passed else "❌ 실패"
                embed = discord.Embed(
                    title=f"{ctx.author.display_name}님 오늘의 인증 결과",
                    description=f"**{status_msg}** (이미 인증 완료)",
                    color=discord.Color.green() if passed else discord.Color.red()
                )
                embed.add_field(name="GitHub", value=f"`{user_data['github_id']}`", inline=True)
                embed.add_field(name="오늘 커밋 / 목표", value=f"**{commits}** / {user_data['goal_per_day']}", inline=True)
                embed.add_field(name="💡 안내", value="하루에 한 번만 인증할 수 있습니다.", inline=False)
                await ctx.send(embed=embed)
                return

            if now_kst.weekday() >= 5:
                await ctx.send("🌴 주말인디 살살하세요 행님 ☕")
                return
            if user_data.get("on_vacation", False):
                await ctx.send("🏝️ 휴가 가서도 코테? 에밥니다 헴")
                return

            # GitHub API 호출로 커밋 수 확인
            commits = await get_valid_commits(bot.http_session, user_data, now_kst)
            if commits is None:
                await ctx.send("❌ GitHub API 호출 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.")
                return
                
            passed = commits >= user_data.get("goal_per_day", 1)
            
            # 데이터베이스에 기록 저장
            await db_update(user_ref, {f"history.{date_str}": {"commits": commits, "passed": passed}})

            result_msg = "✅ 통과! 🎉" if passed else "❌ 커피 한 잔 할래요옹~ 😢"
            embed = discord.Embed(
                title=f"{ctx.author.display_name}님 인증 결과",
                description=f"**{result_msg}**",
                color=discord.Color.green() if passed else discord.Color.red()
            )
            embed.add_field(name="GitHub", value=f"`{user_data['github_id']}`", inline=True)
            embed.add_field(name="오늘 커밋 / 목표", value=f"**{commits}** / {user_data['goal_per_day']}", inline=True)
            embed.add_field(name="📅 인증 시간", value=now_kst.strftime("%H:%M:%S"), inline=True)
            await ctx.send(embed=embed)
            
    except Exception as e:
        logging.error(f"인증 중 오류 발생 (사용자: {user_id}): {e}")
        await ctx.send("❌ 인증 처리 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.")
    finally:
        # 인증 처리 완료 후 상태 해제
        certifying_users.discard(user_id)

@bot.command(name="유저목록")
@commands.cooldown(1, 10, commands.BucketType.channel)  # 채널당 10초 쿨다운
async def user_list(ctx):
    async with ctx.typing():
        try:
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
        except Exception as e:
            logging.error(f"유저목록 조회 중 오류 발생: {e}")
            await ctx.send("❌ 유저 목록 조회 중 오류가 발생했습니다.")

@bot.command(name="삭제")
@commands.has_permissions(administrator=True)
async def delete_user(ctx, member: discord.Member):
    async with ctx.typing():
        try:
            user_ref = db.collection("users").document(str(member.id))
            if not (await db_get(user_ref)).exists:
                await ctx.send("❌ 해당 유저는 등록되어 있지 않습니다.")
                return
            await db_delete(user_ref)
            await ctx.send(f"🗑️ {member.mention} 유저 정보를 삭제했습니다.")
        except Exception as e:
            logging.error(f"유저 삭제 중 오류 발생: {e}")
            await ctx.send("❌ 유저 삭제 중 오류가 발생했습니다.")

@bot.command(name="수정")
@commands.has_permissions(administrator=True)
async def edit_user(ctx, member: discord.Member, key: str, *, value: str):
    async with ctx.typing():
        try:
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
        except Exception as e:
            logging.error(f"유저 수정 중 오류 발생: {e}")
            await ctx.send("❌ 유저 정보 수정 중 오류가 발생했습니다.")

@bot.command(name="기각수정")
@commands.has_permissions(administrator=True)
async def edit_fails(ctx, member: discord.Member, amount: int):
    async with ctx.typing():
        try:
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
            await ctx.send(f"🔧 {member.mention}님의 기각 횟수를 {amount}만큼 조정했습니다. (예상 총 기각: {new_total}회)")
        except Exception as e:
            logging.error(f"기각수정 중 오류 발생: {e}")
            await ctx.send("❌ 기각 수정 중 오류가 발생했습니다.")

@bot.command(name="커피왕")
@commands.cooldown(1, 30, commands.BucketType.channel)  # 채널당 30초 쿨다운
async def coffee_king(ctx):
    async with ctx.typing():
        try:
            users_stream = await db_stream(db.collection("users"))
            ranking = [(s.id, s.to_dict().get("total_fail", 0)) for s in users_stream if s.to_dict().get("total_fail", 0) > 0]
            
            if not ranking:
                await ctx.send("☕ **커피왕 랭킹** ☕\n\n🥳 모두 0잔!? 커피왕이 아니라 코딩왕이셈요 행님덜!")
                return

            ranking.sort(key=lambda x: x[1], reverse=True)
            lines = [f"🏆 **{i+1}위**: <@{uid}> - 누적 **{score}**회" for i, (uid, score) in enumerate(ranking[:10])] # 상위 10명만 표시
            
            embed = discord.Embed(title="☕ 커피왕 랭킹 ☕", description="\n".join(lines), color=discord.Color.dark_gold())
            await ctx.send(embed=embed)
        except Exception as e:
            logging.error(f"커피왕 랭킹 조회 중 오류 발생: {e}")
            await ctx.send("❌ 커피왕 랭킹 조회 중 오류가 발생했습니다.")

@bot.command(name="휴가")
@commands.has_permissions(administrator=True)
async def set_vacation(ctx, member: discord.Member):
    try:
        await db_update(db.collection("users").document(str(member.id)), {"on_vacation": True})
        await ctx.send(f"🏝️ {member.mention} 님을 휴가 상태로 전환했습니다.")
    except Exception as e:
        logging.error(f"휴가 설정 중 오류 발생: {e}")
        await ctx.send("❌ 휴가 설정 중 오류가 발생했습니다.")

@bot.command(name="복귀")
@commands.has_permissions(administrator=True)
async def unset_vacation(ctx, member: discord.Member):
    try:
        await db_update(db.collection("users").document(str(member.id)), {"on_vacation": False})
        await ctx.send(f"👋 {member.mention} 님이 복귀했습니다!")
    except Exception as e:
        logging.error(f"복귀 설정 중 오류 발생: {e}")
        await ctx.send("❌ 복귀 설정 중 오류가 발생했습니다.")

# 날짜를 '월', '화', '수'... 로 바꿔주는 도우미 함수
def get_day_of_week_korean(date_obj):
    days = ["월", "화", "수", "목", "금", "토", "일"]
    return days[date_obj.weekday()]

@bot.command(name="체크")
@commands.cooldown(1, 10, commands.BucketType.user)  # 유저당 10초 쿨다운
async def check_status(ctx):
    """이번 주 자신의 기각 현황을 확인합니다."""
    async with ctx.typing():
        try:
            # --- ✨ 추가된 예외 처리 ---
            today = datetime.now(KST)
            if today.weekday() == 3:  # 오늘이 목요일(weekday=3)인 경우
                embed = discord.Embed(
                    title="🐣 주간 집계 시작!",
                    description=f"오늘은 이번 주 집계가 시작되는 첫날이에요.\n내일부터 현황 조회가 가능합니다!",
                    color=discord.Color.from_rgb(173, 216, 230) # Light Blue
                )
                await ctx.send(embed=embed)
                return
            # --- 여기까지 ---

            user_ref = db.collection("users").document(str(ctx.author.id))
            user_doc = await db_get(user_ref)

            if not user_doc.exists:
                await ctx.send("❌ 먼저 `!등록` 명령어로 등록해주세요.")
                return

            user_data = user_doc.to_dict()
            weekly_fail_count = user_data.get("weekly_fail", 0)

            embed = discord.Embed(title="☕️ 이번 주 나의 기각 현황", color=discord.Color.dark_gold())
            embed.set_author(name=ctx.author.display_name, icon_url=ctx.author.avatar.url if ctx.author.avatar else ctx.author.default_avatar.url)

            if weekly_fail_count == 0:
                embed.description = f"<@{ctx.author.id}> - 누적 **0**회\n\n🥳 우리 행님 코딩 좀 치는디 스벅 고? 행복회로 돌려잇~"
                embed.color = discord.Color.green()
            else:
                history = user_data.get("history", {})
                failed_dates = []

                # 1. 이번 주의 시작(목요일) 날짜 계산
                today = datetime.now(KST)
                # 오늘 요일에서 목요일(3)까지 며칠이 지났는지 계산
                days_since_thursday = (today.weekday() - 3 + 7) % 7
                start_of_week = today.date() - timedelta(days=days_since_thursday)

                # 2. 이번 주 목요일부터 오늘까지의 기록을 확인
                for i in range(7):
                    check_date = start_of_week + timedelta(days=i)
                    # 미래의 날짜는 확인할 필요 없음
                    if check_date > today.date():
                        break
                    
                    date_str = check_date.strftime("%Y-%m-%d")
                    day_record = history.get(date_str)

                    # history에 기록이 있고, passed가 False인 경우
                    if day_record and day_record.get("passed") is False:
                        day_of_week_korean = get_day_of_week_korean(check_date)
                        failed_dates.append(f"**{check_date.strftime('%m/%d')}({day_of_week_korean})**")

                fail_dates_str = ", ".join(failed_dates) if failed_dates else "기록 없음"
                
                embed.description = (
                    f"<@{ctx.author.id}> - 누적 **{weekly_fail_count}**회\n\n"
                    f"**누락 날짜:** {fail_dates_str}\n\n"
                    "😢 행님 누구 하나 키보드 훔치는 건 어때유~"
                )
                embed.color = discord.Color.red()

            await ctx.send(embed=embed)
        except Exception as e:
            logging.error(f"체크 명령어 중 오류 발생: {e}")
            await ctx.send("❌ 현황 조회 중 오류가 발생했습니다.")

# --- 4. 백그라운드 작업 (Tasks) ---

@tasks.loop(minutes=1)
async def daily_check():
    await bot.wait_until_ready()
    try:
        now = datetime.now(KST)
        
        # 주말(토요일=5, 일요일=6)에는 실행하지 않음
        if now.weekday() >= 5:
            return
        
        # 평일 오후 11시 59분에만 실행
        if now.hour == 23 and now.minute == 59:
            logging.info(f"--- 🌙 {now.strftime('%Y-%m-%d')} 일일 기각자 체크 시작 ---")
            users_stream = await db_stream(db.collection("users"))
            channel = bot.get_channel(REPORT_CHANNEL_ID)
            if not channel:
                logging.error("리포트 채널을 찾을 수 없습니다.")
                return
                
            failed_users = []
            date_str = now.strftime("%Y-%m-%d")

            for user_snapshot in users_stream:
                user_id = user_snapshot.id
                user_ref = db.collection("users").document(user_id)
                doc = user_snapshot.to_dict()
                
                if doc.get("on_vacation", False): 
                    continue

                history = doc.get("history", {})
                today_data = history.get(date_str)
                
                # 1. !인증 기록이 있고, 통과(passed: True)한 경우 -> 통과 처리 (아무것도 안 함)
                if today_data and today_data.get("passed", False):
                    continue
                
                # 2. !인증 기록이 없거나, 인증했지만 실패(passed: False)한 경우 -> 기각자 목록에 추가
                failed_users.append(user_id)

                # 3. !인증 기록이 아예 없는 경우에만 DB 기록 및 실패 카운트 증가
                if not today_data:
                    logging.info(f"-> {doc.get('github_id')}님은 인증 기록이 없어 기각 처리됩니다.")
                    # DB에 0커밋, 실패 기록을 저장
                    await db_update(user_ref, {
                        f"history.{date_str}": {"commits": 0, "passed": False}
                    })
                    # 실패 횟수 증가
                    await db_update(user_ref, {
                        "weekly_fail": firestore.Increment(1),
                        "total_fail": firestore.Increment(1)
                    })

            if failed_users:
                mentions = " ".join([f"<@{uid}>" for uid in failed_users])
                await channel.send(f"📢 **[{date_str}] 기각자 목록:**\n{mentions}")
            else:
                await channel.send(f"🎉 **[{date_str}] 전원 통과!** 굿보이 굿걸! 👏")
            
            logging.info(f"--- ✅ 일일 체크 완료: 기각자 {len(failed_users)}명 ---")
    except Exception as e:
        logging.error(f"일일 체크 중 오류 발생: {e}")

@tasks.loop(minutes=1)
async def weekly_reset():
    await bot.wait_until_ready()
    try:
        now = datetime.now(KST)
        
        # 목요일(weekday=3) 자정(00:00)에만 실행
        if now.weekday() == 3 and now.hour == 0 and now.minute == 0:
            logging.info("--- ☕ 주간 커피왕 발표 및 초기화 시작 ---")
            users_stream = await db_stream(db.collection("users"))
            channel = bot.get_channel(REPORT_CHANNEL_ID)
            if not channel:
                logging.error("리포트 채널을 찾을 수 없습니다.")
                return
            
            # 어제(수요일)까지의 데이터를 기준으로 집계
            yesterday = now - timedelta(days=1)
            weekly_fails = {s.id: s.to_dict().get("weekly_fail", 0) for s in users_stream}
            max_fail = max(weekly_fails.values()) if weekly_fails else 0
            
            if max_fail > 0:
                kings = [uid for uid, fails in weekly_fails.items() if fails == max_fail]
                mentions = " ".join([f"<@{uid}>" for uid in kings])
                await channel.send(f"🥶 **이번 주({yesterday.strftime('%m/%d')} 마감) 커피 당첨자 (기각 {max_fail}회):**\n{mentions} !! 음 달다 달아~")
            else:
                await channel.send(f"🎉 **이번 주({yesterday.strftime('%m/%d')} 마감)는 커피왕 없음!** 모두 수고하셨습니다!")

            # 주간 실패 횟수 초기화
            for user_id in weekly_fails.keys():
                await db_update(db.collection("users").document(user_id), {"weekly_fail": 0})
            
            logging.info("--- 📅 주간 실패 횟수 초기화 완료 ---")
    except Exception as e:
        logging.error(f"주간 초기화 중 오류 발생: {e}")


# --- 5. 이벤트 핸들러 및 봇 실행 ---

@bot.event
async def on_ready():
    try:
        bot.http_session = aiohttp.ClientSession()
        logging.info(f"✅ 봇 로그인 완료: {bot.user}")
        daily_check.start()
        weekly_reset.start()
    except Exception as e:
        logging.error(f"봇 시작 중 오류 발생: {e}")

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

# 봇 종료 시 정리 작업
@bot.event
async def on_disconnect():
    logging.info("🔌 봇이 디스코드에서 연결이 끊어졌습니다.")

@bot.event
async def on_resumed():
    logging.info("🔄 봇이 디스코드에 재연결되었습니다.")

async def cleanup():
    """봇 종료 시 정리 작업"""
    try:
        if hasattr(bot, 'http_session') and bot.http_session:
            await bot.http_session.close()
            logging.info("📡 aiohttp 클라이언트 세션 종료됨")
        
        # 진행 중인 작업들 정리
        certifying_users.clear()
        logging.info("🧹 정리 작업 완료")
    except Exception as e:
        logging.error(f"정리 작업 중 오류 발생: {e}")

async def main():
    try:
        async with bot:
            await bot.start(DISCORD_TOKEN)
    except Exception as e:
        logging.error(f"봇 실행 중 오류 발생: {e}")
    finally:
        await cleanup()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("🛑 사용자에 의해 봇이 종료되었습니다.")
    except Exception as e:
        logging.error(f"메인 프로세스 오류: {e}")
    finally:
        logging.info("👋 봇 종료 완료")