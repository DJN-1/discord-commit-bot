import base64

with open("firebaseKey.json", "rb") as f:
    encoded = base64.b64encode(f.read()).decode("utf-8")

with open("firebaseKey.b64", "w") as out:
    out.write(encoded)

print("✅ base64 변환 완료: firebaseKey.b64 파일 생성됨")
