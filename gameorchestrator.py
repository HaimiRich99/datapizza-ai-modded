import os
import yaml
import asyncio
import json
from pathlib import Path
from datapizza.clients.openai_like import OpenAILikeClient
from datapizza.agents.agent import Agent
from datapizza.memory import Memory

class GameOrchestrator:
    def __init__(self, api_key: str, restaurant_id: str):
        self.api_key = api_key
        self.restaurant_id = restaurant_id
        
        # Inizializziamo il Client LLM (Regolo.ai) come richiesto dal regolamento
        self.llm_client = OpenAILikeClient(
            api_key=os.getenv("REGOLO_API_KEY"),
            model="gpt-oss-120b",
            base_url="https://api.regolo.ai/v1",
        )
        
        # La memoria a lungo termine condivisa tra gli agenti
        self.shared_memory = Memory()
        
        self.prompts_dir = Path(__file__).parent / "prompts"
        
        # Inizializziamo la Task Force
        self._init_agents()

    def _load_prompt(self, agent_filename: str) -> str:
        """Legge il system_prompt dal file YAML."""
        file_path = self.prompts_dir / f"{agent_filename}.yaml"
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
                return data.get("system_prompt", "Sei un assistente generico. (ERRORE PROMPT)")
        except FileNotFoundError:
            print(f"🚨 ALLARME: Impossibile trovare il file prompt {file_path}! L'agente sarà muto.")
            return ""

    def _init_agents(self):
        """Istanzia gli agenti caricando i prompt freschi dai file YAML."""
        print("🛠️ Inizializzazione brigata galattica in corso...")
        
        # 1. Agente PR
        pr_prompt = self._load_prompt("pr_agent")
        self.pr_agent = Agent(
            client=self.llm_client,
            system_prompt=pr_prompt,
            tools=[] # TODO: Aggiungeremo i tool di datapizza (send_message, save_menu)
        )
        
        # 2. Agente Squalo
        shark_prompt = self._load_prompt("shark_agent")
        self.shark_agent = Agent(
            client=self.llm_client,
            system_prompt=shark_prompt,
            tools=[] # TODO: Aggiungeremo il tool closed_bid
        )
        
        # 3. Agente Chef
        chef_prompt = self._load_prompt("chef_agent")
        self.chef_agent = Agent(
            client=self.llm_client,
            system_prompt=chef_prompt,
            tools=[] # TODO: Aggiungeremo prepare_dish, serve_dish, update_restaurant_is_open
        )
        
        print("✅ Brigata pronta ai posti di combattimento!")

    async def listen_and_route(self):
        """Il nostro orecchio sempre teso verso il Multiverso."""
        print(f"🎧 Connessione al flusso SSE per il ristorante {self.restaurant_id}...")
        
        import aiohttp
        headers = {"x-api-key": self.api_key}
        # Sostituisci la base URL con quella che ci forniranno su Discord!
        url = f"https://api.hackapizza.com/events/{self.restaurant_id}" 
        
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, headers=headers) as response:
                    if response.status != 200:
                        print(f"🚨 ERRORE SERVER {response.status}: Chiave API errata o ristorante esploso.")
                        return

                    # Ciclo di lettura dello stream SSE
                    async for line in response.content:
                        if not line:
                            continue
                        
                        decoded_line = line.decode('utf-8').strip()
                        if decoded_line.startswith("data:"):
                            data_str = decoded_line[5:].strip()
                            
                            if data_str == "connected":
                                print("🟢 Connessi al server della Federazione! In attesa di eventi...")
                                continue
                                
                            try:
                                event = json.loads(data_str)
                                await self._handle_event(event)
                            except json.JSONDecodeError:
                                print(f"⚠️ Roba non euclidea ricevuta (JSON non valido): {data_str}")
                                continue
            except Exception as e:
                print(f"💥 Connessione interrotta brutalmente: {e}. Tenterò la riconnessione al prossimo ciclo.")

    async def _handle_event(self, event: dict):
        """Il Vigile Urbano. Smista l'evento all'agente di competenza."""
        event_type = event.get("type")
        event_data = event.get("data", {})
        
        # Ignoriamo gli heartbeat per non spammare i log
        if event_type == "heartbeat":
            return
            
        print(f"\n📩 Nuovo Evento SSE Ricevuto: {event_type}")
        
        if event_type == "game_started":
            print("🚀 IL GIOCO È INIZIATO! Reset della memoria e partenza.")
            self.shared_memory.clear() 
            
        elif event_type == "game_phase_changed":
            phase = event_data.get("phase")
            print(f"🔄 Cambio Fase! Entriamo in: {phase.upper()}")
            
            context_prompt = f"Il sistema notifica che è appena iniziata la fase {phase}. Fai la tua mossa."
            
            if phase == "speaking":
                print("🗣️ Passo il microfono all'Agente PR...")
                asyncio.create_task(self._run_agent_async(self.pr_agent, context_prompt))
                
            elif phase == "closed_bid":
                print("🦈 Libero lo Squalo. Andiamo a vincere quest'asta...")
                asyncio.create_task(self._run_agent_async(self.shark_agent, context_prompt))
                
            elif phase == "waiting":
                print("⏳ Fase di attesa. (TODO: Inserire qui il riassunto memoria o il Fixer Agent)")
                # Se hai implementato la compressione della memoria:
                # self.compress_memory_if_needed(threshold=10)
                
            elif phase == "stopped":
                print("🛑 Turno concluso. Ferma le macchine.")
                # self.save_long_term_memory()
                
        elif event_type == "client_spawned":
            # Questa è un'emergenza da Serving Phase!
            client_name = event_data.get("clientName", "Alieno Anonimo")
            order = event_data.get("orderText", "")
            print(f"🛎️ NUOVO CLIENTE! Il tavolo 4 ({client_name}) ha ordinato: {order}")
            
            chef_prompt = f"Nuovo cliente al bancone: {client_name}. Ordine esatto: '{order}'. Prepara e servi il piatto corretto prima che scada il tempo."
            asyncio.create_task(self._run_agent_async(self.chef_agent, chef_prompt))

    async def _run_agent_async(self, agent: Agent, prompt: str):
        """
        Esegue l'agente in un thread separato. 
        MOLTO IMPORTANTE: Impedisce che il loop SSE si blocchi mentre l'LLM genera la risposta!
        """
        print("🧠 L'agente sta elaborando la strategia...")
        try:
            # Usiamo asyncio.to_thread per far girare il metodo invoke (che è sincrono) in background
            # Passiamo sempre la memoria condivisa!
            response = await asyncio.to_thread(
                agent.invoke, 
                prompt, 
                memory=self.shared_memory
            )
            print(f"✅ Agente ha completato l'operazione. Risposta interna: {response.content}")
        except Exception as e:
            print(f"🔥 ERRORE CRITICO: L'agente è andato in kernel panic: {e}")