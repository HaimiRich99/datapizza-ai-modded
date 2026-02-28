import os
import asyncio

# 🪛 Compatibility shim: some versions of the datapizza library use
# asyncio.Runner, which was only added in Python 3.11.  On older
# interpreters we provide a minimal stand‑in so that AsyncExecutor can
# create a loop the way it expects.
if not hasattr(asyncio, "Runner"):
    try:
        asyncio.Runner = asyncio.runners.Runner  # type: ignore
    except AttributeError:
        # create a lightweight replacement matching the minimal subset
        # used by datapizza (context manager exposing get_loop()).
        class _CompatRunner:
            def __init__(self):
                self._loop = asyncio.new_event_loop()

            def __enter__(self):
                # install as current loop for the context
                asyncio.set_event_loop(self._loop)
                return self

            def __exit__(self, exc_type, exc, tb):
                # stop loop when exiting context
                self._loop.close()

            def get_loop(self):
                return self._loop

        asyncio.Runner = _CompatRunner  # type: ignore
        # note: this doesn't replicate every feature of the real Runner,
        # but AsyncExecutor only uses it to create a loop and call
        # run_forever() on it, which is satisfied.


from gameorchestrator import GameOrchestrator

async def main():
    print("🚀 Inizializzazione sistema Hackapizza 2.0...")
    
    # 1. Recupero delle variabili d'ambiente (chiavi e ID)
    # Suggerimento: usa python-dotenv per caricare un file .env locale!
    API_KEY = os.getenv("API_KEY", "dTpZhKpZ02-64ce4aa45c9d63abe32944e1")
    RESTAURANT_ID = os.getenv("RESTAURANT_ID", 24)
    REGOLO_KEY = os.getenv("REGOLO_API_KEY", "sk-jlEroWKZpj7abgk2vQzPxA")
    
    if not all([API_KEY, RESTAURANT_ID, REGOLO_KEY]):
        print("🛑 ERRORE FATALE: Variabili d'ambiente mancanti.")
        print("Assicurati di aver impostato")
        return

    # 2. Istanziamo il cervello del nostro ristorante
    orchestrator = GameOrchestrator(
        api_key=API_KEY,  # type: ignore
        restaurant_id=RESTAURANT_ID  # type: ignore
    )
    
    # 3. Lanciamo l'ascolto infinito degli eventi SSE
    try:
        await orchestrator.listen_and_route()
    except KeyboardInterrupt:
        print("\n🛑 Spegnimento manuale del bot. Arrivederci al prossimo ciclo cosmico!")

if __name__ == "__main__":
    # Esegue il loop degli eventi asincroni di Python
    asyncio.run(main())