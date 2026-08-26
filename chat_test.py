import sys
import os
import mimetypes
from google import genai
from google.genai import types
from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown

console = Console()

console.print("\n[bold magenta]TarsusAI Terminal Test Aracı Başlatılıyor...[/bold magenta]\n", justify="center")

try:
  # Vertex AI ile başlatıyoruz
  client = genai.Client(vertexai=True)
except Exception as e:
  console.print(f"[bold red]Hata: Vertex AI başlatılamadı:[/bold red] {e}")
  sys.exit(1)

# Sistem komutunu (System Prompt) main.py ile senkronize ettik.
# Böylece terminaldeki testleriniz ile web arayüzündeki cevaplar birebir aynı kalitede olacak!
system_prompt = (
    "Sen TarsusAI bünyesinde çalışan, Çukurova ve Tarsus bölgesinde uzmanlaşmış kıdemli bir Uzman Ziraat Mühendisisin. "
    "Görevin, çiftçilere ve bahçe sahiplerine bilimsel, tarım bakanlığı onaylı, pratik ve verim artırıcı tavsiyeler vermektir.\n\n"
    "Tarsus Bölgesine Özel Uzmanlık Bilgilerin:\n"
    "1. Tarsus Beyazı Üzümü (Prasutgili): Genellikle Mart-Nisan aylarında budanır. Külleme hastalığına karşı çiçeklenme öncesi ve sonrasında kükürt uygulaması önerilir.\n"
    "2. Sarıulak Zeytini: Tarsus'un tescilli zeytinidir. Zeytin sineği zararlısına karşı Haziran ve Eylül aylarında tuzaklar veya ilaçlama kontrol edilmelidir. Sulama çiçeklenme döneminde çok kritiktir.\n"
    "3. Tarsus Pamuğu ve Narenciye: Çukurova sıcağında damlama sulama sistemleri önerilir. Narenciyede unlu bit zararlısına karşı biyolojik mücadele (faydalı böcek kullanımı) teşvik edilmelidir.\n\n"
    "Konuşma Kuralların:\n"
    "- Çiftçilere karşı samimi, saygılı, babacan ve her zaman profesyonel bir ziraat mühendisi tonuyla konuş.\n"
    "- Tarımsal terimleri açıklayarak anlat (örneğin 'NPK gübresi' dediğinde azot, fosfor, potasyum olduğunu belirt).\n"
    "- Her cevabında verimi artırmaya ve toprağı korumaya yönelik çevre dostu tavsiyeler ver.\n"
    "- EĞER çiftçi bir FOTOĞRAF/GÖRSEL gönderdiyse: Fotoğraftaki bitkiyi, yaprağı, meyveyi, zararlıyı veya lekeyi çok dikkatli incele. Yapraklardaki sararmalar, mantar lekeleri veya böcek hasarlarına bakarak ziraat mühendisi hassasiyetiyle teşhis koy ve tedavi adımlarını reçete gibi yaz."
)

config = types.GenerateContentConfig(
  system_instruction=system_prompt,
  temperature=0.6, # main.py ile uyumlu olması için yaratıcılığı 0.6 yaptık
)

console.print(Panel(
  "[bold green]TarsusAI Canlı Terminal Sohbeti Aktif![/bold green]\n\n"
  "👉 [bold cyan]Yazı ile sohbet etmek için:[/bold cyan] Doğrudan sorunuzu yazın.\n"
  "👉 [bold magenta]Fotoğraf analizi testi için:[/bold magenta] [yellow]/resim <dosya_yolu> <sorunuz>[/yellow] şeklinde yazın.\n"
  "   [italic dim](Örn: /resim yaprak.jpg bu lekeler neden kaynaklanıyor olabilir?)[/italic dim]\n\n"
  "❌ Çıkmak için [bold red]'exit'[/bold red] yazabilirsiniz.",
  title="[bold yellow]Sistem Hazır (Mühendis Modu)[/bold yellow]",
  subtitle="Model: Gemini 2.5 Flash"
))

def load_local_image(file_path: str):
    """Yerel diskteki resmi okuyup bytes ve mime type döndürür."""
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Görsel belirtilen yolda bulunamadı: '{file_path}'")

    mime_type, _ = mimetypes.guess_type(file_path)
    if not mime_type:
        mime_type = "image/jpeg" # default fallback

    with open(file_path, "rb") as f:
        image_bytes = f.read()
    return image_bytes, mime_type

while True:
  try:
    user_message = console.input("\n[bold cyan]Siz > [/bold cyan]").strip()
    if user_message.lower() in ["exit", "quit", "çıkış"]:
      console.print("[bold red]Terminal sohbeti sonlandırıldı.[/bold red]")
      break

    if not user_message:
      continue

    contents = []

    # Gelişmiş Komut: Görsel Analiz Testi
    if user_message.startswith("/resim"):
        parts = user_message.split(maxsplit=2)
        if len(parts) < 2:
            console.print("[bold red]Hata: Lütfen bir görsel yolu belirtin! Örn: /resim yaprak.jpg[/bold red]")
            continue

        file_path = parts[1]
        custom_question = parts[2] if len(parts) > 2 else "Lütfen bu görseldeki bitkiyi analiz edip teşhis koyun."

        try:
            with console.status(f"[bold magenta]Görsel yükleniyor: {file_path}...[/bold magenta]"):
                img_bytes, mime_type = load_local_image(file_path)
                contents.append(
                    types.Part.from_bytes(
                        data=img_bytes,
                        mime_type=mime_type
                    )
                )
            contents.append(custom_question)
            console.print(f"[bold green]✔ Görsel başarıyla yüklendi ({mime_type}). Analiz başlıyor...[/bold green]")
        except Exception as e:
            console.print(f"[bold red]Resim yükleme hatası:[/bold red] {e}")
            continue
    else:
        # Normal Yazı Sohbeti
        contents.append(user_message)

    # Yapay zeka düşünüyor animasyonu
    with console.status("[bold yellow]TarsusAI düşünüyor...[/bold yellow]"):
      response = client.models.generate_content(
        model='gemini-2.5-flash',
        contents=contents,
        config=config
      )

    # Yanıtı Markdown formatında güzel bir panel içinde göster
    console.print(Panel(
      Markdown(response.text),
      title="[bold violet]Ziraat Mühendisi Tavsiyesi[/bold violet]",
      border_style="violet",
      padding=(1, 2)
    ))
  except KeyboardInterrupt:
    console.print("\n[bold red]Sohbet durduruldu.[/bold red]")
    break
  except Exception as e:
    console.print(f"[bold red]Hata oluştu:[/bold red] {e}")
