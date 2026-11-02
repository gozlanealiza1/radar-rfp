# Radar RFP Globecast

Une page web qui regroupe **automatiquement, chaque matin**, les appels d'offres qui concernent Globecast.
Personne n'a rien à faire à la main : les sales ouvrent simplement le lien de la page.

**Sources :** TED (Europe), BOAMP (France), Find a Tender (Royaume-Uni), SAM.gov (États-Unis, en option)
et Google Actualités pour les acteurs privés (beIN, Canal+…).
**Onglets :** Nouveautés · Ouverts · Comptes cibles · Signaux presse · Historique.

Tout tourne gratuitement sur GitHub : ni sur un PC, ni sur une boîte mail.

---

## Installation (environ 15 minutes, une seule fois)

### 1. Créer un compte GitHub
Va sur **github.com** et clique sur **Sign up**. Si Globecast ou Orange a déjà un GitHub d'entreprise, utilise-le.

### 2. Créer le projet
- En haut à droite, clique sur **+** puis **New repository**.
- Repository name : `radar-rfp`
- Coche **Public** (indispensable pour que la page soit gratuite, voir la note en bas).
- Clique sur **Create repository**.

### 3. Déposer les fichiers
- Sur la page du projet, clique sur le lien **uploading an existing file**.
- Dézippe le dossier reçu, puis glisse-dépose **tout son contenu**
  (`radar.py`, `config.yaml`, `requirements.txt`, `README.md`, et les dossiers `site` et `docs`).
- Clique sur **Commit changes**.

### 4. Ajouter le fichier d'automatisation
Ce fichier est dans un dossier caché, qui part rarement en glisser-déposer. On le crée donc à la main :
- Clique sur **Add file** puis **Create new file**.
- Dans le nom du fichier, tape exactement : `.github/workflows/radar.yml`
- Ouvre le fichier `radar.yml` du dossier reçu (dans `.github/workflows/`) avec le Bloc-notes, copie tout et colle-le.
  Si tu ne vois pas ce dossier, affiche les fichiers cachés ou demande-moi le contenu.
- Clique sur **Commit changes**.

### 5. Autoriser l'automatisation à enregistrer ses résultats
- Va dans **Settings**, puis **Actions**, puis **General**.
- Tout en bas, dans *Workflow permissions*, coche **Read and write permissions**.
- Clique sur **Save**.

### 6. Mettre la page en ligne
- Va dans **Settings**, puis **Pages**.
- Dans *Source*, choisis **Deploy from a branch**.
- Dans *Branch*, choisis **main** et le dossier **/docs**, puis clique sur **Save**.
- Au bout d'une minute, l'adresse de la page s'affiche en haut,
  du type `https://ton-nom.github.io/radar-rfp/`.

### 7. Lancer le premier passage
- Va dans l'onglet **Actions**, clique sur **Radar RFP** à gauche, puis **Run workflow**.
- Attends que le rond devienne vert (5 à 15 minutes au premier passage, qui récupère l'historique).
- Ouvre l'adresse de la page : elle est remplie.

C'est fini. Le radar repasse ensuite **tout seul chaque matin**.

### 8. Partager avec les sales
Envoie-leur l'adresse de la page et demande-leur de la mettre en favori. Ils n'ont besoin d'aucun compte.

---

## Modifier la veille
Tous les réglages sont dans **`config.yaml`** : mots-clés, mots exclus, codes CPV, comptes cibles, recherches presse.
Sur GitHub : clique sur le fichier, puis sur l'icône **crayon**, modifie, et clique sur **Commit changes**.
Le changement est pris en compte au passage suivant (ou tout de suite avec **Run workflow**).

## Activer les États-Unis (optionnel)
1. Crée un compte gratuit sur **sam.gov**, puis demande une *Public API Key* dans ton profil.
2. Sur GitHub, va dans **Settings**, puis **Secrets and variables**, puis **Actions**, et clique sur **New repository secret**.
   Nom : `SAM_API_KEY` · Valeur : ta clé.

## Si une source est en erreur
En bas de la page, *État des sources* indique pour chaque source si elle est à jour, et affiche le message d'erreur sinon.
Une source en panne ne bloque pas les autres.

## Note sur la confidentialité
Avec un compte GitHub gratuit, le projet et la page sont **publics**. L'adresse n'est pas référencée
dans les moteurs de recherche, mais n'importe qui qui la connaît peut l'ouvrir.
Les données affichées sont publiques (appels d'offres officiels). En revanche, `config.yaml` montre
quels mots-clés et quels comptes vous suivez.
Pour une page privée, il faut un GitHub d'entreprise (Team ou Enterprise), avec lequel les mêmes étapes fonctionnent en mode **Private**.
