"""Robô de monitoramento de passagens GRU -> Japão (2 adultos, econômica).

Roda no GitHub Actions 4x/dia. Enquanto a data de volta ainda não está à venda,
acompanha o preço só da ida (e testa 1x/dia se a volta já liberou). Depois, cada execução faz 2 buscas no SerpApi (Google Flights),
salva o resultado em data/ e abre uma "issue" de ALERTA (o GitHub te avisa por e-mail)
quando aparece preço total abaixo de LIMITE_ALERTA.
"""
import csv
import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

API_KEY = os.environ["SERPAPI_KEY"]
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GH_REPO = os.environ.get("GITHUB_REPOSITORY", "")
LIMITE_ALERTA = 12000.0
TZ = ZoneInfo("America/Sao_Paulo")

# horário local (aprox.) -> data de ida
SLOTS = {3: "2027-10-09", 8: "2027-10-10", 15: "2027-10-11", 20: "2027-10-12"}

agora = dt.datetime.now(TZ)
hoje = agora.date()
os.makedirs("data", exist_ok=True)


def log(msg):
    print(f"[{agora:%Y-%m-%d %H:%M}] {msg}")


def serpapi(params):
    params = {**params, "engine": "google_flights", "adults": 2, "currency": "BRL",
              "hl": "pt", "gl": "br", "api_key": API_KEY}
    url = "https://serpapi.com/search.json?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=90) as r:
        return json.load(r)


def conta():
    url = "https://serpapi.com/account.json?api_key=" + API_KEY
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)


def resumir(resp):
    voos = (resp.get("best_flights") or []) + (resp.get("other_flights") or [])
    voos = [v for v in voos if v.get("price")]
    voos.sort(key=lambda v: v["price"])
    top = []
    for v in voos[:3]:
        segs = v.get("flights", [])
        top.append({
            "preco": v["price"],
            "cias": sorted({s.get("airline", "?") for s in segs}),
            "trajeto": " > ".join([segs[0]["departure_airport"]["id"]] +
                                  [s["arrival_airport"]["id"] for s in segs]) if segs else "",
            "duracao_h": round(v.get("total_duration", 0) / 60, 1),
        })
    return top


def abrir_alerta(reg):
    if not (GH_TOKEN and GH_REPO):
        return False
    t = reg["top"][0]
    titulo = (f"🚨 ALERTA: GRU–Japão por R$ {reg['menor_preco']:,.0f} (2 pessoas) – "
              f"{reg['rota']} {reg['ida']} a {reg['volta']}").replace(",", ".")
    corpo = (f"**Rota:** {reg['rota']}\n**Ida:** {reg['ida']}  **Volta:** {reg['volta']} "
             f"({reg['dias']} dias)\n**Preço total (2 adultos):** R$ {reg['menor_preco']:,.0f}\n"
             f"**Cias:** {', '.join(t['cias'])}\n**Trajeto (ida):** {t['trajeto']}\n"
             f"**Duração (ida):** {t['duracao_h']} h\n\n[Abrir no Google Flights]({reg['link']})\n").replace(",", ".")
    req = urllib.request.Request(
        f"https://api.github.com/repos/{GH_REPO}/issues",
        data=json.dumps({"title": titulo, "body": corpo}).encode(),
        headers={"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=30):
        return True


def oj(dest, ida, volta):
    return {"type": 3, "multi_city_json": json.dumps([
        {"departure_id": "GRU", "arrival_id": dest, "date": ida},
        {"departure_id": "HND,NRT", "arrival_id": "GRU", "date": volta}])}


def so_ida(dest, ida):
    return {"type": 2, "departure_id": "GRU", "arrival_id": dest, "outbound_date": ida}


def buscar(rota, params, ida, volta, dia):
    """Faz 1 busca, registra no histórico e abre alerta se for o caso."""
    dias = (dt.date.fromisoformat(volta) - dt.date.fromisoformat(ida)).days if volta else ""
    reg = {"quando": agora.strftime("%Y-%m-%d %H:%M"), "rota": rota, "ida": ida,
           "volta": volta or "", "dias": dias, "menor_preco": None, "top": [], "link": "",
           "status": "ok", "alerta": False}
    try:
        resp = serpapi(params)
        reg["link"] = resp.get("search_metadata", {}).get("google_flights_url", "")
        if resp.get("error"):
            reg["status"] = "datas ainda não liberadas" if "hasn't returned" in resp["error"] else resp["error"]
        else:
            reg["top"] = resumir(resp)
            reg["menor_preco"] = reg["top"][0]["preco"] if reg["top"] else None
    except Exception as e:  # noqa: BLE001
        reg["status"] = f"erro: {e}"
    log(f"{rota} {ida}→{volta or '(só ida)'}: {reg['menor_preco']} ({reg['status']})")

    p = reg["menor_preco"]
    # alerta só vale para ida+volta (preço total da viagem)
    if volta and p and p < LIMITE_ALERTA and (dia["alerta_min"] is None or p < dia["alerta_min"]):
        try:
            reg["alerta"] = abrir_alerta(reg)
            dia["alerta_min"] = p
        except Exception as e:  # noqa: BLE001
            log(f"Falha ao abrir alerta: {e}")

    dia["buscas"].append(reg)
    novo = not os.path.exists("data/historico.csv")
    with open("data/historico.csv", "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if novo:
            w.writerow(["quando", "rota", "ida", "volta", "dias", "menor_preco", "cias", "status", "link"])
        w.writerow([reg["quando"], rota, ida, volta or "", dias, p or "",
                    "/".join(reg["top"][0]["cias"]) if reg["top"] else "", reg["status"], reg["link"]])
    return reg


def main():
    hora = min(SLOTS, key=lambda h: abs(h - agora.hour))
    ida = SLOTS[hora]
    volta = "2027-11-01" if hoje.day % 2 else "2027-10-31"

    restantes = conta().get("total_searches_left", 0)
    if restantes < 3:
        log(f"Cota do SerpApi quase no fim ({restantes}). Pulando.")
        return

    estado = json.load(open("data/estado.json")) if os.path.exists("data/estado.json") else {"volta_liberada": False}
    arq_dia = f"data/{hoje.isoformat()}.json"
    dia = json.load(open(arq_dia)) if os.path.exists(arq_dia) else {"buscas": [], "alerta_min": None}

    if not estado["volta_liberada"] and hora == 3:
        # 1x por dia (02:52): testa se a data de volta já está à venda
        r = buscar("Osaka→Tóquio", oj("KIX", ida, volta), ida, volta, dia)
        if r["menor_preco"]:
            estado["volta_liberada"] = True
            log("Datas de volta liberadas! A partir de agora: buscas de ida e volta.")

    if estado["volta_liberada"]:
        # modo completo: ida+volta (2 buscas por execução)
        feitas = [b for b in dia["buscas"] if b["quando"] == agora.strftime("%Y-%m-%d %H:%M")]
        if not feitas:
            buscar("Osaka→Tóquio", oj("KIX", ida, volta), ida, volta, dia)
        if hora == 20 and hoje.weekday() == 6:  # domingo à noite: Hiroshima
            buscar("Hiroshima→Tóquio", oj("HIJ", "2027-10-09", volta), "2027-10-09", volta, dia)
        else:
            buscar("Tóquio ida e volta", {"type": 1, "departure_id": "GRU", "arrival_id": "HND,NRT",
                                          "outbound_date": ida, "return_date": volta}, ida, volta, dia)
    else:
        # modo "só ida": enquanto a volta não está à venda, acompanha o preço da ida
        r = buscar("Só ida GRU→Osaka", so_ida("KIX", ida), ida, None, dia)
        if r["status"] != "datas ainda não liberadas" and hora != 3:
            buscar("Só ida GRU→Tóquio", so_ida("HND,NRT", ida), ida, None, dia)

    json.dump(estado, open("data/estado.json", "w"), ensure_ascii=False, indent=1)
    dia["modo"] = "ida e volta" if estado["volta_liberada"] else "só ida (volta ainda não à venda)"
    dia["cota_restante"] = conta().get("total_searches_left")
    json.dump(dia, open(arq_dia, "w"), ensure_ascii=False, indent=1)
    json.dump(dia, open("data/hoje.json", "w"), ensure_ascii=False, indent=1)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print("ERRO:", e)
        sys.exit(1)
