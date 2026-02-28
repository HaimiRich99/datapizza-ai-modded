import yaml
import aiohttp
from pathlib import Path

def load_yaml_prompt(filename: str, prompts_dir: str = "prompts") -> str:
    """Legge e restituisce il system_prompt da un file YAML."""
    file_path = Path(prompts_dir) / f"{filename}.yaml"
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
            return data.get("system_prompt", "")
    except FileNotFoundError:
        print(f"🚨 ALLARME: File {file_path} non trovato! Agente muto.")
        return ""

async def fetch_api_data(base_url: str, endpoint: str, api_key: str, params: dict | None = None) -> dict:
    """Funzione helper generica per le chiamate GET alla Federazione."""
    url = f"{base_url}{endpoint}"
    headers = {"x-api-key": api_key}
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers, params=params) as response:
                if response.status == 200:
                    return await response.json()
                print(f"⚠️ Errore API su {endpoint}: {response.status} - {await response.text()}")
                return {}
        except Exception as e:
            print(f"🔥 Errore di rete su {endpoint}: {e}")
            return {}