import discord
from discord.ext import commands, tasks
import os
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, firestore
import datetime
import requests
import pytz

# .env 불러오기
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
REPORT_CHANNEL_ID = int(os.getenv("REPORT_CHANNEL_ID"))

# Firebase 초기화
cred = credentials.Certificate("firebaseKey.json")
firebase_admin.initialize_app(cred)
db = firestore.client()

# 디스코드 클라이언트 설정
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# 한국 시간대
KST = pytz.timezone("Asia/Seoul")

# !등록 명령어 (관리자만 사용 가능)
@bot.command()
@commands.has_permissions(administrator=True)
async def 등록(ctx, discord_mention: str, github_id: str, repo_name: str, goal_per_day: int):
    if discord_mention.startswith('<@') and discord_mention.endswith('>'):
        discord_id = discord_mention.replace('<@', '').replace('>', '').replace('!', '')
    else:
        discord_id = discord_mention

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

# !인증 명령어
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
    history = data.get("history", {}).get(today_str)

    if history:
        commits = history.get("commits", 0)
        passed = history.get("passed", False)
    else:
        since = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        url = f"https://api.github.com/repos/{github_id}/{repo}/commits?author={github_id}&since={since}"
        headers = {"Accept": "application/vnd.github.v3+json"}
        response = requests.get(url, headers=headers)
        commits = len(response.json()) if isinstance(response.json(), list) else 0
        passed = commits >= goal

        user_ref.update({
            f"history.{today_str}": {"commits": commits, "passed": passed}
        })

    result_msg = "✅ 통과! 🎉" if passed else "❌ 기각 😢"
    await ctx.send(
        f"{result_msg}\n"
        f"👤 GitHub: {github_id}\n"
        f"📦 Repo: {repo}\n"
        f"📅 오늘 커밋: {commits} / 목표: {goal}"
    )

# !유저목록 명령어
@bot.command()
async def 유저목록(ctx):
    users = db.collection("users").stream()
    lines = []
    for user in users:
        doc = user.to_dict()
        lines.append(f"🧑 {user.id} → {doc.get('github_id')} / {doc.get('repo_name')} / 목표 {doc.get('goal_per_day')}회")

    await ctx.send("📋 등록된 유저 목록:\n" + "\n".join(lines) if lines else "등록된 유저가 없습니다.")

# !삭제 명령어
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

# !수정 명령어
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

# !기각수정 명령어
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
        await ctx.send(f"🛠️ <@{discord_id}> 기각 수 수정 완료: {updates}")
    else:
        await ctx.send("⚠️ 수정할 내용이 없습니다. 최소 1개 이상 입력해주세요.")

# 매일 자정 체크
@tasks.loop(minutes=1)
async def daily_check():
    now = datetime.datetime.now(KST)
    if now.hour == 0 and now.minute == 0:
        today_str = now.strftime("%Y-%m-%d")
        users = db.collection("users").stream()
        channel = bot.get_channel(REPORT_CHANNEL_ID)
        message_lines = []

        for user in users:
            doc = user.to_dict()
            passed = doc.get("history", {}).get(today_str, {}).get("passed", False)
            if not passed:
                db.collection("users").document(user.id).update({
                    "weekly_fail": firestore.Increment(1),
                    "total_fail": firestore.Increment(1)
                })
                message_lines.append(f"❌ {user.id} 기각")

        if message_lines:
            await channel.send("📢 오늘의 기각자 목록:\n" + "\n".join(message_lines))

# 매주 목요일 00:00 정산
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

# !커피왕 명령어
@bot.command()
async def 커피왕(ctx):
    users = db.collection("users").stream()
    ranking = [(user.id, user.to_dict().get("total_fail", 0)) for user in users]
    if not ranking or all(fail == 0 for _, fail in ranking):
        await ctx.send("☕ 커피왕 랭킹 ☕\n🥳 모두 0잔! 커밋 열심히 하셨습니다!")
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

# 봇 시작 이벤트
@bot.event
async def on_ready():
    print(f"✅ 봇 로그인 완료: {bot.user}")
    daily_check.start()
    weekly_reset.start()

bot.run(DISCORD_TOKEN)
