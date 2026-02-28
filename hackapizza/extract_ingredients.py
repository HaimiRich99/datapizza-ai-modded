import json

def conta_ingredienti_unici(file_path):
    with open(file_path, 'r', encoding='utf-8') as f:
        recipes = json.load(f)

    set_ingredienti = set()

    for recipe in recipes:
        # Estrae le chiavi (i nomi degli ingredienti) dal dizionario 'ingredients'
        nomi_ingr = recipe.get('ingredients', {}).keys()
        set_ingredienti.update(nomi_ingr)

    return sorted(list(set_ingredienti))

if __name__ == "__main__":
    ingredienti = conta_ingredienti_unici('recipes.json')
    
    print(f"Numero totale di ingredienti unici trovati: {len(ingredienti)}")
    
    # Salviamo la lista per verifica
    with open('lista_completa_ingredienti.txt', 'w', encoding='utf-8') as f:
        for item in ingredienti:
            f.write(f"{item}\n")