import discord
from discord.ext import commands, tasks
import os
import base64
import json
import logging
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, firestore
import datetime
import requests
import pytz

# 로깅 설정
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] [%(levelname)s] %(message)s')
logging.info("✅ main.py 실행 시작됨")

# .env 로드
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    logging.warning("❌ DISCORD_TOKEN 누락됨!")

REPORT_CHANNEL_ID = int(os.getenv("REPORT_CHANNEL_ID"))

# Firebase 키 base64 변환
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

@bot.command()
async def 인증(ctx):
    discord_id = str(ctx.author.id)
    user_ref = db.collection("users").document(discord_id)
    doc = user_ref.get()

    if not doc.exists:
        await ctx.send("❌ 먼저 !등록 명령어로 등록해주세요.")
        return

    data = doc.to_dict()
    github_id = data["github_id"]
    repo = data["repo_name"]
    goal = data["goal_per_day"]

    now = datetime.datetime.now(KST)
    today_str = now.strftime("%Y-%m-%d")

    utc_since = datetime.datetime.utcnow().replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat() + "Z"

    url = f"https://api.github.com/repos/{github_id}/{repo}/commits?since={utc_since}"
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "Authorization": f"Bearer {os.getenv('GITHUB_TOKEN')}"
    }

    logging.info(f"📡 인증 요청 URL: {url}")
    response = requests.get(url, headers=headers)
    logging.info(f"📡 응답 코드: {response.status_code}")
    logging.info(f"📡 응답 일부: {response.text[:300]}")

    if response.status_code != 200:
        await ctx.send("❌ GitHub API 호출 실패: 사용자 또는 레포 확인")
        return

    try:
        all_commits = response.json()
    except Exception as e:
        logging.warning(f"❌ JSON 파싱 실패: {e}")
        await ctx.send("❌ GitHub 응답이 올바르지 않습니다.")
        return

    commits = sum(
        1 for c in all_commits
        if github_id.lower() in {
            c.get("author", {}).get("login", "").lower(),
            c.get("committer", {}).get("login", "").lower()
        }
    )
    passed = commits >= goal

    user_ref.update({
        f"history.{today_str}": {
            "commits": commits,
            "passed": passed
        }
    })

    result_msg = "✅ 통과! 🎉" if passed else "❌ 커피 한 잔 할래요옹~ 😢"
    await ctx.send(
        f"{result_msg}\n"
        f"👤 GitHub: {github_id}\n"
        f"📦 Repo: {repo}\n"
        f"📅 오늘 커밋: {commits} / 목표: {goal}"
    )

@tasks.loop(minutes=1)
async def daily_check():
    now = datetime.datetime.now(KST)
    if now.weekday() < 5 and now.hour == 0 and now.minute == 0:
        today_str = now.strftime("%Y-%m-%d")
        users = db.collection("users").stream()
        channel = bot.get_channel(REPORT_CHANNEL_ID)
        message_lines = []

        for user in users:
            doc = user.to_dict()
            history = doc.get("history", {})
            today_data = history.get(today_str)
            passed = today_data.get("passed") if today_data else False

            if not passed:
                db.collection("users").document(user.id).update({
                    "weekly_fail": firestore.Increment(1),
                    "total_fail": firestore.Increment(1)
                })
                message_lines.append(f"❌ <@{user.id}> 기각")

        if message_lines:
            await channel.send("📢 오늘의 기각자 목록:\n" + "\n".join(message_lines))
        else:
            await channel.send("🎉 오늘은 모두 통과했습니다. 굿보이 굿걸 👏")

@tasks.loop(minutes=1)
async def weekly_reset():
    now = datetime.datetime.now(KST)
    if now.weekday() == 3 and now.hour == 0 and now.minute == 0:
        users = db.collection("users").stream()
        channel = bot.get_channel(REPORT_CHANNEL_ID)
        message_lines = ["☕ 주간 커밋 정산 결과 (평일 기준)"]
        survivors, losers = [], []

        for user in users:
            doc = user.to_dict()
            user_id = user.id
            weekly_fail = doc.get("weekly_fail", 0)

            if weekly_fail < 5:
                losers.append((user_id, weekly_fail))
            else:
                survivors.append(user_id)

            db.collection("users").document(user_id).update({"weekly_fail": 0})

        if losers:
            message_lines.append("🥶 커피 당첨자 (평일 기각 5회 미만):")
            for uid, count in losers:
                message_lines.append(f"- <@{uid}> ({count}회 기각)")
        else:
            message_lines.append("🎉 전원 생존! 모두 커밋을 지켰습니다!")

        await channel.send("\n".join(message_lines))

@bot.event
async def on_ready():
    logging.info(f"✅ 봇 로그인 완료: {bot.user}")
    daily_check.start()
    weekly_reset.start()

bot.run(DISCORD_TOKEN)
