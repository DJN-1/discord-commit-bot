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
    KST = pytz.timezone("Asia/Seoul")
    now_kst = datetime.datetime.now(KST)

    # 주말 제외
    if now_kst.weekday() >= 5:
        await ctx.send("🌴 오늘은 주말입니다. 셀프 칭찬하세욥 ☕")
        return

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
    today_str = now_kst.strftime("%Y-%m-%d")

    # KST 자정 기준 -> UTC 변환
    today_start_kst = now_kst.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end_kst = today_start_kst + datetime.timedelta(days=1)

    since_utc = today_start_kst.astimezone(pytz.utc).isoformat()
    until_utc = today_end_kst.astimezone(pytz.utc).isoformat()

    url = f"https://api.github.com/repos/{github_id}/{repo}/commits?since={since_utc}&until={until_utc}"
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "Authorization": f"Bearer {os.getenv('GITHUB_TOKEN')}"
    }

    logging.info(f"📡 인증 요청 URL: {url}")
    response = requests.get(url, headers=headers)
    logging.info(f"📡 응답 코드: {response.status_code}")
    logging.info(f"📡 응답 일부: {response.text[:300]}")
    logging.info(f"📡 Rate Limit: {response.headers.get('X-RateLimit-Remaining')}/{response.headers.get('X-RateLimit-Limit')}, Reset={response.headers.get('X-RateLimit-Reset')}")

    if response.status_code != 200:
        await ctx.send("❌ GitHub API 호출 실패: 사용자 또는 레포 확인")
        return

    try:
        all_commits = response.json()
    except Exception as e:
        logging.warning(f"❌ JSON 파싱 실패: {e}")
        await ctx.send("❌ GitHub 응답이 올바르지 않습니다.")
        return

    valid_commits = []
    for c in all_commits:
        commit_time_str = c.get("commit", {}).get("committer", {}).get("date", "")
        if not commit_time_str:
            continue

        try:
            commit_time_utc = datetime.datetime.fromisoformat(commit_time_str.replace("Z", "+00:00"))
            commit_time_kst = commit_time_utc.astimezone(KST)
        except Exception as e:
            logging.warning(f"⛔ 시간 파싱 실패: {commit_time_str} - {e}")
            continue

        if commit_time_kst.date() != now_kst.date():
            continue  # 오늘 KST 날짜 아님

        author_login = c.get("author", {}).get("login", "").lower()
        committer_login = c.get("committer", {}).get("login", "").lower()
        sha = c.get("sha", "")[:7]

        if github_id.lower() in {author_login, committer_login}:
            valid_commits.append((sha, commit_time_str))
            logging.info(f"🕒 커밋 확인: SHA={sha}, UTC={commit_time_str}, KST={commit_time_kst.strftime('%Y-%m-%d %H:%M:%S')}")

    commits = len(valid_commits)
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
    logging.info(f"✅ 봇 로그인 완료: {bot.user}")
    initialize_daily_history.start()
    daily_check.start()
    weekly_reset.start()

bot.run(DISCORD_TOKEN)
