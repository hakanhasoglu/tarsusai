import os
import base64
import io
from typing import Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from google import genai
from google.genai import types

# Görsel doğrulama için Pillow kütüphanesi
try:
    from PIL import Image
    pillow_available = True
except ImportError:
    pillow_available = False

# Terminal loglarını güzelleştirmek için zengin kütüphanelerimiz
from rich.console import Console
from rich.panel import Panel

console = Console()
app = FastAPI(title="TarsusAI - Akıllı Tarım Asistanı")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

try:
    client = genai.Client(vertexai=True)
    console.print("[bold green]✔ Vertex AI bağlantısı başarıyla kuruldu![/bold green]")
except Exception as e:
    console.print(f"[bold red]❌ Vertex AI başlatılamadı:[/bold red] {e}")
    client = None

# İstek modeli
class ChatRequest(BaseModel):
    message: Optional[str] = ""
    image: Optional[str] = None # Base64 formatında görsel

def parse_base64_image(base64_str: str):
    """
    HTML arayüzünden gelen Base64 formatındaki görsel verisini temizler,
    decode eder ve mime_type ile birlikte döndürür.
    """
    try:
        console.print(f"[yellow]Gelen ham görsel karakter uzunluğu:[/yellow] {len(base64_str)}")
        
        if "," in base64_str:
            header, base64_data = base64_str.split(",", 1)
        else:
            header, base64_data = "", base64_str
        
        # Mime Type Ayıklama (Örn: image/jpeg, image/png)
        mime_type = "image/jpeg"
        if "data:" in header and ";base64" in header:
            mime_type = header.split(";")[0].replace("data:", "")
            
        # HTTP aktarımındaki olası boşluk/onarım hatalarını düzeltelim
        base64_data = base64_data.strip().replace("\n", "").replace("\r", "").replace(" ", "+")
        
        image_bytes = base64.b64decode(base64_data)
        console.print(f"[green]✔ Görsel başarıyla decode edildi. Boyut:[/green] {len(image_bytes)} byte. [green]Tür:[/green] {mime_type}")
        
        if len(image_bytes) == 0:
            raise ValueError("Çözülen görsel verisi tamamen boş (0 byte) çıktı.")
            
        return image_bytes, mime_type
    except Exception as e:
        console.print(f"[bold red]❌ Base64 Çözümleme Hatası:[/bold red] {str(e)}")
        raise ValueError(f"Görsel çözümlenirken hata oluştu: {str(e)}")

@app.get("/")
async def get_index():
    return FileResponse("index.html")

@app.post("/api/chat")
async def chat_endpoint(request: ChatRequest):
    if not client:
        raise HTTPException(status_code=500, detail="Vertex AI bağlantısı aktif değil.")
    
    # Terminale ilk isteği loglayalım
    log_message = f"[bold cyan]Çiftçi Sorusu:[/bold cyan] {request.message or '[Sadece Görsel Gönderildi]'}"
    if request.image:
        log_message += "\n[bold magenta]📷 Görsel Eki Tespit Edildi. İşleniyor...[/bold magenta]"
        
    console.print(Panel(
        log_message, 
        title="[yellow]Tarım Danışma Talebi[/yellow]",
        border_style="yellow"
    ))

    try:
        # Yapay zekaya uzmanlık seviyesinde ziraat bilgisi aşılıyoruz
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
            temperature=0.6,
        )

        # Gemini'ye gönderilecek içerik listesi
        contents = []

        # Eğer görsel varsa işleyelim
        if request.image:
            image_bytes, mime_type = parse_base64_image(request.image)
            
            # Pillow yüklüyse görseli PIL nesnesi olarak göndermek en güvenli/kararlı yoldur.
            if pillow_available:
                try:
                    pil_image = Image.open(io.BytesIO(image_bytes))
                    contents.append(pil_image)
                    console.print(f"[bold green]✔ Görsel Pillow (PIL) nesnesine başarıyla çevrildi. Boyut: {pil_image.size}[/bold green]")
                except Exception as img_err:
                    console.print(f"[bold red]⚠ PIL Çevrim Hatası:[/bold red] {img_err}. Klasik byte yöntemine geçiliyor.")
                    contents.append(
                        types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
                    )
            else:
                # Pillow yüklü değilse düz byte olarak ekle
                contents.append(
                    types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
                )

        # Çiftçinin metin mesajını ekleyelim
        if request.message:
            contents.append(request.message)
        else:
            # Sadece fotoğraf atıldıysa varsayılan soruyu biz ekliyoruz
            contents.append("Lütfen bu bitki görselindeki hastalığı/durumu teşhis et ve çözüm önerilerini paylaş.")

        console.print(f"[yellow]Gemini'ye gönderilen toplam içerik (parts) sayısı:[/yellow] {len(contents)}")

        # Gemini modelini çağırıyoruz
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=contents,
            config=config
        )

        console.print(Panel(
            f"[bold green]Mühendis Cevabı:[/bold green] {response.text}", 
            title="[violet]Ziraat Mühendisi Tavsiyesi[/violet]",
            border_style="violet"
        ))

        return {"response": response.text}
    except Exception as e:
        console.print(f"[bold red]FastAPI endpoint hatası:[/bold red] {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))