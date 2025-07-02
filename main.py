import discord
from discord.ext import commands, tasks
import os
import base64
import json
import logging
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, firestore
from datetime import datetime, timedelta
import datetime
import requests
import pytz
from dateutil import parser

# 로깅 설정
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] [%(levelname)s] %(message)s')
logging.info("✅ main.py 실행 시작됨")

# .env 로드
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    logging.warning("❌ DISCORD_TOKEN 누락됨!")

REPORT_CHANNEL_ID = int(os.getenv("REPORT_CHANNEL_ID"))

firebase_key_base64 = os.getenv("FIREBASE_KEY_BASE64")
logging.info(f"📦 FIREBASE_KEY_BASE64 길이: {len(firebase_key_base64 or '')}")

if not firebase_key_base64:
    raise ValueError("❌ 환경변수 FIREBASE_KEY_BASE64가 누락되었습니다!")

cred_dict = json.loads(base64.b64decode(firebase_key_base64).decode("utf-8"))
cred = credentials.Certificate(cred_dict)
firebase_admin.initialize_app(cred)
db = firestore.client()

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

KST = pytz.timezone("Asia/Seoul")

def get_user_data(discord_id):
    doc = db.collection("users").document(discord_id).get()
    return doc.to_dict() if doc.exists else None


async def get_valid_commits(user, now_kst):
    start_kst = now_kst.replace(hour=0, minute=0, second=0, microsecond=0)
    end_kst = start_kst + timedelta(days=1)
    since = (start_kst - timedelta(days=1)).astimezone(pytz.utc).isoformat()
    until = (end_kst + timedelta(days=1)).astimezone(pytz.utc).isoformat()

    url = f"https://api.github.com/repos/{user['github_id']}/{user['repo_name']}/commits?since={since}&until={until}"
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "Authorization": f"Bearer {os.getenv('GITHUB_TOKEN')}"
    }

    res = requests.get(url, headers=headers)
    logging.info(f"📡 GitHub 인증 요청 → 사용자: {user['github_id']}, 상태: {res.status_code}")

    if res.status_code != 200:
        logging.warning(f"❌ GitHub API 호출 실패\n응답: {res.text}")
        return 0

    all_commits = res.json()
    valid_count = 0

    for c in all_commits:
        time_str = c.get("commit", {}).get("committer", {}).get("date")
        sha = c.get("sha", "")[:7]

        if not time_str:
            logging.warning(f"⛔ 타임스탬프 누락된 커밋: SHA={sha}")
            continue

        try:
            time_kst = parser.isoparse(time_str).astimezone(KST)
        except Exception as e:
            logging.warning(f"⛔ 시간 파싱 실패: {time_str} - {e}")
            continue

        if time_kst.date() != now_kst.date():
            logging.info(f"📅 제외된 커밋: SHA={sha}, 날짜={time_kst.strftime('%Y-%m-%d')} (오늘 아님)")
            continue

        author_login = c.get("author", {}).get("login", "").lower()
        committer_login = c.get("committer", {}).get("login", "").lower()
        if user["github_id"].lower() in {author_login, committer_login}:
            valid_count += 1
            logging.info(f"✅ 유효 커밋 {valid_count}: SHA={sha}, KST={time_kst.strftime('%Y-%m-%d %H:%M:%S')}")

    return valid_count


def is_valid_commit(commit, github_id, target_date):
    time_str = commit.get("commit", {}).get("committer", {}).get("date")
    if not time_str:
        return False
    try:
        time_kst = parser.isoparse(time_str).astimezone(KST)
    except:
        return False
    if time_kst.date() != target_date:
        return False

    author_login = commit.get("author", {}).get("login", "").lower()
    committer_login = commit.get("committer", {}).get("login", "").lower()
    return github_id.lower() in {author_login, committer_login}


async def update_daily_history(discord_id, date_obj, count, passed):
    date_str = date_obj.strftime("%Y-%m-%d")
    db.collection("users").document(str(discord_id)).update({
        f"history.{date_str}": {
            "commits": count,
            "passed": passed
        }
    })


def format_result_msg(user, commits, passed):
    result_msg = "✅ 통과! 🎉" if passed else "❌ 커피 한 잔 할래요옹~ 😢"
    return (
        f"{result_msg}\n"
        f"👤 GitHub: {user['github_id']}\n"
        f"📦 Repo: {user['repo_name']}\n"
        f"📅 오늘 커밋: {commits} / 목표: {user['goal_per_day']}"
    )

@bot.command()
@commands.has_permissions(administrator=True)
async def 등록(ctx, discord_mention: str, github_id: str, repo_name: str, goal_per_day: int):
    discord_id = discord_mention.replace('<@', '').replace('>', '').replace('!', '') if discord_mention.startswith('<@') else discord_mention

    repo_url = f"https://api.github.com/repos/{github_id}/{repo_name}"
    user_url = f"https://api.github.com/users/{github_id}"
    repo_res = requests.get(repo_url)
    user_res = requests.get(user_url)

    if repo_res.status_code != 200 or user_res.status_code != 200:
        await ctx.send("❌ 존재하지 않는 GitHub 사용자 또는 레포입니다.")
        return

    user_ref = db.collection("users").document(discord_id)
    if user_ref.get().exists:
        await ctx.send(f"⚠️ <@{discord_id}> 은(는) 이미 등록된 사용자입니다.")
        return

    user_ref.set({
        "github_id": github_id,
        "repo_name": repo_name,
        "goal_per_day": goal_per_day,
        "history": {},
        "weekly_fail": 0,
        "total_fail": 0
    })
    await ctx.send(f"✅ <@{discord_id}> 등록 완료 - {github_id}/{repo_name}, {goal_per_day}회/일")

is_first_cert_call = True  # 모듈 상단 또는 함수 바깥에서 선언

@bot.command()
async def 인증(ctx):
    global is_first_cert_call
    logging.info(f"📥 [!인증 진입] 호출자: {ctx.author.display_name} / 핸들러 ID: {id(인증)}")

    if is_first_cert_call:
        logging.warning("⚠️ [디버그] 첫 인증 호출로 감지됨! 중복 발생 여부 체크 중")
        is_first_cert_call = False

    try:
        discord_id = str(ctx.author.id)
        logging.info(f"[인증 시작] 디스코드 ID: {discord_id} / 닉네임: {ctx.author.display_name}")

        user_data = get_user_data(discord_id)
        if not user_data:
            await ctx.send("❌ 먼저 !등록 명령어로 등록해주세요.")
            return

        now_kst = datetime.datetime.now(KST)
        logging.info(f"[인증 시간] 현재 시간 (KST): {now_kst.strftime('%Y-%m-%d %H:%M:%S')}")

        if now_kst.weekday() >= 5:
            logging.info("[인증 종료] 주말 - 인증 면제")
            await ctx.send("🌴 오늘은 주말입니다. 셀프 칭찬하세욥 ☕")
            return

        commits = await get_valid_commits(user_data, now_kst)
        passed = commits >= user_data["goal_per_day"]

        logging.info(f"[인증 결과] 유효 커밋 수: {commits} / 목표: {user_data['goal_per_day']} / 통과: {passed}")
        await update_daily_history(discord_id, now_kst.date(), commits, passed)

        logging.info(f"[ctx.send 호출 전] 사용자: {discord_id}")
        await ctx.send(format_result_msg(user_data, commits, passed))
        logging.info(f"[ctx.send 완료] 메시지 전송됨")

    except Exception as e:
        logging.exception("⛔ 인증 처리 중 예외 발생")
        await ctx.send("❌ 인증 처리 중 문제가 발생했어요. 관리자에게 문의해주세요.")

@bot.command()
async def 유저목록(ctx):
    users = db.collection("users").stream()
    lines = []
    for user in users:
        doc = user.to_dict()
        lines.append(f"🧑 {doc.get('github_id')} / {doc.get('repo_name')} / 목표 {doc.get('goal_per_day')}회")

    await ctx.send("📋 등록된 유저 목록:\n" + "\n".join(lines) if lines else "등록된 유저가 없습니다.")

@bot.command()
@commands.has_permissions(administrator=True)
async def 삭제(ctx, discord_mention: str):
    discord_id = discord_mention.replace('<@', '').replace('>', '').replace('!', '') if discord_mention.startswith('<@') else discord_mention
    user_ref = db.collection("users").document(discord_id)
    if user_ref.get().exists:
        user_ref.delete()
        await ctx.send(f"🗑️ <@{discord_id}> 유저 삭제 완료")
    else:
        await ctx.send("❌ 해당 유저가 존재하지 않습니다.")

@bot.command()
@commands.has_permissions(administrator=True)
async def 수정(ctx, discord_mention: str, github_id: str = None, repo_name: str = None, goal_per_day: int = None):
    discord_id = discord_mention.replace('<@', '').replace('>', '').replace('!', '') if discord_mention.startswith('<@') else discord_mention
    user_ref = db.collection("users").document(discord_id)
    doc = user_ref.get()
    if not doc.exists:
        await ctx.send("❌ 해당 유저가 존재하지 않습니다.")
        return

    updates = {}
    if github_id:
        updates["github_id"] = github_id
    if repo_name:
        updates["repo_name"] = repo_name
    if goal_per_day is not None:
        updates["goal_per_day"] = goal_per_day

    user_ref.update(updates)
    await ctx.send(f"🔧 <@{discord_id}> 유저 정보 수정 완료: {updates}")

@bot.command()
@commands.has_permissions(administrator=True)
async def 기각수정(ctx, discord_mention: str, weekly_fail: int = None, total_fail: int = None):
    discord_id = discord_mention.replace('<@', '').replace('>', '').replace('!', '') if discord_mention.startswith('<@') else discord_mention
    user_ref = db.collection("users").document(discord_id)
    doc = user_ref.get()
    if not doc.exists:
        await ctx.send("❌ 해당 유저가 존재하지 않습니다.")
        return

    updates = {}
    if weekly_fail is not None:
        updates["weekly_fail"] = weekly_fail
    if total_fail is not None:
        updates["total_fail"] = total_fail

    if updates:
        user_ref.update(updates)
        await ctx.send(f"🛠️ <@{discord_id}> 기각 수수수수퍼노바 : {updates}")
    else:
        await ctx.send("⚠️ 수정할 내용이 없습니다. 최소 1개 이상 입력해주세요.")

@bot.command()
async def 커피왕(ctx):
    users = db.collection("users").stream()
    ranking = [(user.id, user.to_dict().get("total_fail", 0)) for user in users]
    if not ranking or all(fail == 0 for _, fail in ranking):
        await ctx.send("☕ 커피왕 랭킹 ☕\n🥳 모두 0잔! 커피왕 아니고 코딩왕!!!")
        return

    ranking.sort(key=lambda x: x[1], reverse=True)
    result = "☕ 커피왕 랭킹 ☕\n"
    prev_score = None
    current_rank = 0
    shown_count = 0

    for i, (uid, score) in enumerate(ranking):
        if score == 0:
            continue
        if score != prev_score:
            current_rank = shown_count + 1
        result += f"{current_rank}위: <@{uid}> - 누적 기각 {score}회\n"
        prev_score = score
        shown_count += 1

    await ctx.send(result)

@tasks.loop(minutes=1)
async def initialize_daily_history():
    now = datetime.datetime.now(KST)
    if now.weekday() < 5 and now.hour == 0 and now.minute == 0:
        today_str = now.strftime("%Y-%m-%d")
        users = db.collection("users").stream()
        for user in users:
            db.collection("users").document(user.id).update({
                f"history.{today_str}": {
                    "commits": 0,
                    "passed": False
                }
            })
        logging.info(f"📅 {today_str} 기록 초기화 완료")

@tasks.loop(minutes=1)
async def daily_check():
    now = datetime.datetime.now(KST)
    if now.weekday() < 5 and now.hour == 23 and now.minute == 59:
        target_date = now.strftime("%Y-%m-%d")
        users = db.collection("users").stream()
        channel = bot.get_channel(REPORT_CHANNEL_ID)
        message_lines = []

        for user in users:
            doc = user.to_dict()
            history = doc.get("history", {})
            today_data = history.get(target_date)
            passed = today_data.get("passed") if today_data else None

            if passed is not True:
                db.collection("users").document(user.id).update({
                    "weekly_fail": firestore.Increment(1),
                    "total_fail": firestore.Increment(1)
                })
                message_lines.append(f"❌ <@{user.id}> 기각")

        if message_lines:
            await channel.send(f"📢 [{target_date}] 기각자 목록:\n" + "\n".join(message_lines))
        else:
            await channel.send(f"🎉 [{target_date}] 모두 통과! 굿보이 굿걸 👏")

@tasks.loop(minutes=1)
async def weekly_reset():
    now = datetime.datetime.now(KST)
    if now.weekday() == 3 and now.hour == 0 and now.minute == 0:
        users = db.collection("users").stream()
        channel = bot.get_channel(REPORT_CHANNEL_ID)
        message_lines = ["☕ 주간 커피왕 발표 ☕"]

        max_fail = -1
        coffee_king_ids = []

        for user in users:
            doc = user.to_dict()
            user_id = user.id
            weekly_fail = doc.get("weekly_fail", 0)

            if weekly_fail >= 1:
                if weekly_fail > max_fail:
                    max_fail = weekly_fail
                    coffee_king_ids = [user_id]
                elif weekly_fail == max_fail:
                    coffee_king_ids.append(user_id)

            db.collection("users").document(user_id).update({"weekly_fail": 0})

        if coffee_king_ids:
            message_lines.append(f"🥶 이번 주 커피 당첨자 (기각 {max_fail}회):")
            for uid in coffee_king_ids:
                message_lines.append(f"- <@{uid}>")
        else:
            message_lines.append("🎉 모두 주 1회 이상 기각되지 않음! 이번 주는 커피왕 없음 ☕")

        await channel.send("\n".join(message_lines))

@bot.event
async def on_ready():
    logging.info(f"✅ [on_ready] 봇 로그인 완료: {bot.user}")
    
    logging.info("[on_ready] 초기화 루프 시작: initialize_daily_history")
    initialize_daily_history.start()

    logging.info("[on_ready] 초기화 루프 시작: daily_check")
    daily_check.start()

    logging.info("[on_ready] 초기화 루프 시작: weekly_reset")
    weekly_reset.start()

bot.run(DISCORD_TOKEN)
