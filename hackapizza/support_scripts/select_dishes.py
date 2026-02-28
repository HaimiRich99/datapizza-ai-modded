import json

def estrai_ricette_indipendenti(file_path):
    # Apriamo il file JSON originale
    with open(file_path, 'r', encoding='utf-8') as f:
        recipes = json.load(f)

    ingredienti_usati = set()
    ricette_selezionate = []

    # Ordiniamo le ricette per numero di ingredienti (dal minore al maggiore)
    # per massimizzare il numero di piatti indipendenti.
    recipes.sort(key=lambda x: len(x['ingredients']))

    # Scorriamo tutte le ricette
    for recipe in recipes:
        ingredienti_ricetta = set(recipe['ingredients'].keys())

        # Controlliamo se ci sono intersezioni con quelli già usati
        if not ingredienti_ricetta.intersection(ingredienti_usati):
            ricette_selezionate.append(recipe)
            ingredienti_usati.update(ingredienti_ricetta)

    return ricette_selezionate

if __name__ == "__main__":
    file_input = 'recipes.json'
    file_output = 'piatti_distanti.json'
    
    # Esecuzione della logica
    risultato = estrai_ricette_indipendenti(file_input)
    
    # Salvataggio nel nuovo file JSON
    with open(file_output, 'w', encoding='utf-8') as f_out:
        json.dump(risultato, f_out, indent=4, ensure_ascii=False)
    
    # Feedback a video
    print(f"Analisi completata!")
    print(f"Trovate {len(risultato)} ricette con ingredienti indipendenti.")
    print(f"Il risultato è stato salvato in: {file_output}")