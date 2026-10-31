# Huquq quiz boti

Aiogram 3 va SQLite asosidagi Telegram guruh quiz boti. Bot huquq fanidan har kuni avtomatik quiz o‘tkazadi, javoblarni qabul qiladi, ballarni saqlaydi va reytingni ko‘rsatadi.

## Imkoniyatlar

- Har kuni 20:00 da e’lon, 20:05 da avtomatik quiz (Toshkent vaqti).
- 8–11 sinflar navbat bilan yoki bitta sinfga fiksatsiya qilinadi.
- Bir savolga 20–30 soniya, inline A/B/C/D tugmalari.
- Har bir foydalanuvchi bir savolga faqat bir marta javob beradi.
- Har savoldan keyin to‘g‘ri javob, izoh va manba.
- Savol yakunida nechta kishi javob bergani va nechtasi to‘g‘ri javob bergani.
- Quiz oxirida qatnashchilar reytingi.
- SQLite bazada user statistikasi va umumiy reyting.
- Rasmli savollar, `image_file_id` orqali.
- Admin uchun savol qo‘shish, rasm biriktirish va JSON import qilish.
- Log fayli: `logs/bot.log`.

## Papka tarkibi

| Fayl | Vazifasi |
|---|---|
| `main.py` | Bot, quiz oqimi, scheduler va admin buyruqlari |
| `config.py` | `.env` sozlamalarini o‘qish va tekshirish |
| `database.py` | SQLite jadvallari va so‘rovlar |
| `questions.json` | Boshlang‘ich savollar bazasi |
| `import_questions.py` | JSON savollarni bazaga import qilish |
| `requirements.txt` | Python paketlari |
| `.env.example` | Sozlamalar namunasi |
| `huquq_quiz.db` | Bot ishlaganda yaratiladigan baza |
| `logs/bot.log` | Log fayli |

Eski `quiz.db` ishlatilmaydi. Yangi bot `huquq_quiz.db` yaratadi, shuning uchun eski baza bilan ziddiyat bo‘lmaydi.

## 1. O‘rnatish

PowerShellni loyiha papkasida oching:

```powershell
Set-Location "C:\Users\Legion\Desktop\лоьоль"
```

Virtual muhit allaqachon `venv` papkasida bo‘lsa, uni qayta yaratish shart emas. Bo‘lmasa:

```powershell
python -m venv venv
```

Virtual muhitni yoqish:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\venv\Scripts\Activate.ps1
```

Paketlarni o‘rnatish:

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## 2. `.env` sozlash

`.env` fayli mavjud bo‘lmasa:

```powershell
Copy-Item .env.example .env
```

Keyin:

```powershell
notepad .env
```

Namuna:

```dotenv
BOT_TOKEN=PASTE_YOUR_TOKEN_HERE
ADMIN_IDS=123456789
GROUP_CHAT_ID=
QUIZ_GRADE=0
QUIZ_GRADES=8,9,10,11
QUESTIONS_PER_QUIZ=5
QUESTION_SECONDS=25
ANNOUNCE_HOUR=20
ANNOUNCE_MINUTE=0
QUIZ_HOUR=20
QUIZ_MINUTE=5
RECENT_QUESTION_DAYS=30
TIMEZONE=Asia/Tashkent
DB_PATH=huquq_quiz.db
QUESTIONS_FILE=questions.json
LOG_FILE=logs/bot.log
```

Muhim sozlamalar:

- `BOT_TOKEN` — BotFather bergan token. Tokenni hech kimga yubormang va GitHubga qo‘ymang.
- `ADMIN_IDS` — adminlarning Telegram ID lari, vergul bilan: `111111111,222222222`.
- `GROUP_CHAT_ID` — ixtiyoriy. Guruhda `/setgroup` ishlatsangiz, avtomatik saqlanadi.
- `QUIZ_GRADE=0` — 8–11 sinflar navbat bilan. `QUIZ_GRADE=9` — faqat 9-sinf.
- `QUESTION_SECONDS` — 20 dan 30 gacha.
- `QUESTIONS_PER_QUIZ` — bitta quizdagi savollar soni.
- `TIMEZONE=Asia/Tashkent`.

Eski `.env` faylida `TZ=Asia/Tashkent` bo‘lsa, uni `TIMEZONE=Asia/Tashkent` bilan almashtiring.

## 3. Savollarni bazaga yuklash

```powershell
python import_questions.py
```

Faqat tekshirish uchun:

```powershell
python import_questions.py --dry-run
```

Import `code` maydoni bo‘yicha idempotent: bir xil savol qayta import qilinsa, yangilanadi va dublikat yaratilmaydi.

## 4. Botni ishga tushirish

```powershell
python main.py
```

To‘xtatish uchun `Ctrl+C`.

Birinchi ishga tushishda `huquq_quiz.db` va `logs/bot.log` avtomatik yaratiladi.

## 5. Guruhni sozlash

1. Botni Telegram guruhga qo‘shing.
2. Botni guruh administratori qiling. Xabar yuborish, rasm yuborish va tugmalar ishlashi uchun bu muhim.
3. Guruhda `/setgroup` yuboring. Guruh avtomatik saqlanadi.
4. Sinov uchun `/quiznow 9` yuboring.

Agar kerak bo‘lsa, admin private chatda chat ID bilan ham sozlashi mumkin:

```text
/setgroup -1001234567890
```

## 6. Buyruqlar

Foydalanuvchilar uchun:

| Buyruq | Vazifasi |
|---|---|
| `/start` | Bot haqida ma’lumot |
| `/stats` | Shaxsiy natijalar |
| `/top` | Umumiy reyting |

Adminlar uchun:

| Buyruq | Vazifasi |
|---|---|
| `/quiznow [sinf]` | Quizni darhol boshlash. Sinf ko‘rsatilmasa, bugungi sinf olinadi |
| `/cancelquiz` | Faol quizni bekor qilish |
| `/setgroup` | Joriy guruhni quiz guruhi sifatida saqlash |
| `/addquestion` | Yangi savol qo‘shish. Faqat private chatda |
| `/setimage` | Savolga rasm biriktirish yoki olib tashlash. Faqat private chatda |
| `/reload` | `questions.json` ni qayta import qilish |
| `/fileid` | Rasmga reply qilib, `file_id` olish |

### Savol qo‘shish

Private chatda:

```text
/addquestion
```

Bot ketma-ket so‘raydi:

1. Sinf: `9`
2. Savol matni
3. 4 ta variant, har biri yangi qatorda
4. To‘g‘ri javob: `A`, `B`, `C` yoki `D`
5. Izoh
6. Manba
7. Rasm yoki `/skip`

Bekor qilish: `/cancel`.

### Rasm biriktirish

```text
/setimage
```

Savol ID yoki `code` ni kiriting, keyin rasm yuboring. Rasmni olib tashlash uchun `/skip`.

## 7. Avtomatik jadval

`config.py` va `.env` asosida:

- 20:00 — e’lon
- 20:05 — quiz

Vaqt `Asia/Tashkent` bo‘yicha hisoblanadi. Bot o‘chgan vaqtda quiz o‘tkazib yuboriladi. Bot 5 daqiqa ichida qayta ishga tushsa, APScheduler `misfire_grace_time` orqali jobni bajarishga harakat qiladi.

Windowsda bot doim ishlashi uchun Task Scheduler:

```powershell
$Action = New-ScheduledTaskAction `
  -Execute "C:\Users\Legion\Desktop\лоьоль\venv\Scripts\python.exe" `
  -Argument "C:\Users\Legion\Desktop\лоьоль\main.py" `
  -WorkingDirectory "C:\Users\Legion\Desktop\лоьоль"

$Trigger = New-ScheduledTaskTrigger -AtStartup

Register-ScheduledTask `
  -TaskName "HuquqQuizBot" `
  -Action $Action `
  -Trigger $Trigger `
  -Description "Huquq fanidan Telegram quiz boti" `
  -RunLevel Highest
```

Bu buyruq administrator huquqi bilan ishlaydi. Agar kerak bo‘lmasa, Task Scheduler oynasida oddiy “At log on” triggerini tanlang.

Tekshirish:

```powershell
Get-ScheduledTask -TaskName "HuquqQuizBot"
Start-ScheduledTask -TaskName "HuquqQuizBot"
```

O‘chirish:

```powershell
Unregister-ScheduledTask -TaskName "HuquqQuizBot" -Confirm:$false
```

## 8. Savollar bazasi va manbalar

`questions.json` da 40 ta boshlang‘ich savol bor: 8, 9, 10 va 11-sinflar uchun 10 tadan.

Savollar quyidagi rasmiy va o‘quv manbalariga asoslangan:

- O‘zbekiston Respublikasi Konstitutsiyasi: https://lex.uz/docs/-6445145
- Konstitutsiyaning rasmiy sayti: https://constitution.uz/oz/clause/index
- O‘zbekiston Respublikasi Jinoyat kodeksi: https://lex.uz/docs/-111453
- Ma’muriy javobgarlik to‘g‘risidagi kodeks: https://lex.uz/acts/-97664
- Fuqarolik kodeksi: https://lex.uz/docs/-111189
- Oila kodeksi: https://lex.uz/docs/-104720
- Mehnat kodeksi: https://lex.uz/docs/-6257288
- BMT hujjatlari: https://www.un.org/en/about-us/universal-declaration-of-human-rights
- Bola huquqlari konvensiyasi: https://www.unicef.org/child-rights-convention
- Jeneva konvensiyalari: https://www.icrc.org/en/document/geneva-conventions
- Maktab Huquq dasturi: https://uzbmb.uz/page/huquq_dastur

Yangi savol qo‘shganda manbani aniq yozing. Manbasiz yoki tekshirilmagan huquqiy faktni bazaga kiritmang.

JSON formati:

```json
{
  "code": "g9-11",
  "grade": 9,
  "text": "Savol matni?",
  "option_a": "Birinchi variant",
  "option_b": "Ikkinchi variant",
  "option_c": "Uchinchi variant",
  "option_d": "To‘rtinchi variant",
  "correct_option": 2,
  "explanation": "Qisqa izoh.",
  "source": "Manba nomi va havolasi",
  "image_file_id": null
}
```

`correct_option`: `0` = A, `1` = B, `2` = C, `3` = D.

## 9. Ma’lumotlar bazasi

Jadvallar:

- `questions` — savollar
- `quiz_sessions` — quiz sessiyalari
- `session_questions` — sessiyadagi savollar va vaqtlar
- `answers` — har bir foydalanuvchi javobi
- `participants` — sessiya bo‘yicha ballar
- `users` — umumiy foydalanuvchi statistikasi
- `settings` — guruh chat ID kabi sozlamalar

Bazani zaxiralash uchun botni to‘xtatib, `huquq_quiz.db` faylini nusxalang.

## 10. Muammolarni bartaraf etish

### `BOT_TOKEN topilmadi`

`.env` fayli loyiha papkasida borligini va `BOT_TOKEN` to‘ldirilganini tekshiring.

### `BOT_TOKEN hali namuna qiymatda`

`.env.example` dagi `PASTE_YOUR_TOKEN_HERE` o‘rniga haqiqiy token yozing.

### `Chat not found` yoki bot guruhga yozmayapti

- Bot guruhga qo‘shilganini tekshiring.
- Bot guruh administratori bo‘lsin.
- `/setgroup` ni guruhning o‘zida qayta yuboring.
- `GROUP_CHAT_ID` manfiy son bo‘lishi kerak.

### `Vaqt zonasi topilmadi`

```powershell
python -m pip install tzdata
```

### `Unauthorized`

Token noto‘g‘ri yoki bekor qilingan. BotFather orqali yangi token oling.

### PowerShell `.ps1` ishlamayapti

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

### Bot ikki marta javob bermayapti

Bu normal: bir foydalanuvchi bir savolga faqat bir marta javob bera oladi.

### Savol chiqmayapti

```powershell
python import_questions.py
python -c "from database import Database; print(Database('huquq_quiz.db').question_counts())"
```

## 11. Xavfsizlik

- `.env` faylini GitHubga yoki boshqa joyga yubormang.
- Token biron joyda ochiq ko‘rsatilgan bo‘lsa, BotFatherda `/revoke` qilib yangi token oling.
- Admin ID larini faqat ishonchli odamlarga bering.
- `logs/bot.log` faylida foydalanuvchi IDlari bo‘lishi mumkin, uni ommaviy joylashtirmang.
- `.env.example` faqat namuna bo‘lishi kerak, unda haqiqiy token turmasin.
