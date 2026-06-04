# E-Commerce Serverless SAGA Purchase Funnel (OpenFaaS)

Ce projet implémente une architecture e-commerce hautement disponible et résiliente, reposant sur le pattern **SAGA Événementiel**. Le workflow d'achat est orchestré à travers 4 fonctions Serverless sur **OpenFaaS** écrites en Python 3, s'appuyant sur des conteneurs PostgreSQL (base d'états) et MinIO (Object Storage compatible S3 pour les factures), et notifiant les événements critiques sur un Webhook Discord.

---

## 1. Architecture Générale & Machine d'États

Le workflow SAGA se déroule comme suit :

1. **`checkout-gateway`** (Synchrone - REST API) : Reçoit le panier de l'utilisateur, insère la commande en base de données avec le statut `PENDING_PAYMENT`, et envoie immédiatement une requête asynchrone à la passerelle OpenFaaS (`/async-function/payment-reconciler`) avant de renvoyer un code `201 Created` avec l'UUID de la commande.
2. **`payment-reconciler`** (Asynchrone - Queue Worker NATS) : Simule une réconciliation de paiement.
   - **Succès** : Met à jour la commande à `PAID` et déclenche de façon asynchrone la fonction `invoice-archiver`.
   - **Échec** : Met à jour la commande à `FAILED` et arrête le workflow.
3. **`invoice-archiver`** (Asynchrone - Queue Worker NATS) : Récupère les informations de commande, génère une facture au format JSON, l'archive dans le bucket MinIO sous `factures/YYYY-MM/invoice_[order_id].json`, passe la commande au statut `INVOICED` en y rattachant l'URL publique de téléchargement, et enfin alerte le canal Discord via un webhook.
4. **`order-history-api`** (Synchrone - REST API) : Expose un endpoint GET pour interroger l'historique des commandes d'un utilisateur (`?user_id=X`) ou par statut (`?status=Y`).

### Machine d'États de la table `orders`
```
       [ Panier Reçu ]
              │
              ▼
       PENDING_PAYMENT
              │
       ┌──────┴──────┐ (Payment Reconciler)
       ▼             ▼
     PAID          FAILED (Fin SAGA / Compensé)
       │
       ▼ (Invoice Archiver)
   INVOICED (S3 URL attachée)
```

---

## 2. Déploiement de l'Infrastructure (PostgreSQL, MinIO & Secrets)

Pour lancer PostgreSQL et MinIO localement et configurer le cluster Kubernetes/OpenFaaS, nous fournissons le script automatisé `02-infrastructure-setup.sh`.

### Lancement Automatisé
Exécutez simplement la commande suivante :
```bash
./02-infrastructure-setup.sh
```

Ce script effectue automatiquement les actions suivantes :
1. Démarre un conteneur PostgreSQL (`saga-postgres`) exposé sur le port `5432` et exécute le schéma d'initialisation `database/init.sql`.
2. Démarre un conteneur MinIO (`saga-minio`) avec le port d'API `9000` et la console sur le port `9001`.
3. Initialise le bucket MinIO `invoices`.
4. Crée le namespace `openfaas-fn` sur Kubernetes.
5. Crée les 3 secrets Kubernetes indispensables dans le namespace `openfaas-fn` pour que les fonctions OpenFaaS puissent communiquer avec l'hôte externe (via l'adresse DNS `host.docker.internal`).

### Commandes Manuelles de création des Secrets
Si vous souhaitez changer vos clés d'API ou injecter un vrai Webhook Discord pour vos tests de correction académique, exécutez la commande suivante en modifiant l'URL :

```bash
kubectl create secret generic api-credentials \
  --from-literal=payment-api-key="sk_test_mock_saga_key_2026" \
  --from-literal=webhook-url="VOTRE_WEBHOOK_DISCORD_REEL" \
  -n openfaas-fn --dry-run=client -o yaml | kubectl apply -f -
```

Les secrets de base de données et S3 sont créés de la même manière :
```bash
# PostgreSQL
kubectl create secret generic db-credentials \
  --from-literal=host="host.docker.internal" \
  --from-literal=port="5432" \
  --from-literal=user="postgres" \
  --from-literal=password="postgres-password" \
  --from-literal=dbname="saga_ecommerce" \
  -n openfaas-fn --dry-run=client -o yaml | kubectl apply -f -

# MinIO/S3
kubectl create secret generic s3-credentials \
  --from-literal=endpoint="http://host.docker.internal:9000" \
  --from-literal=access_key="minioadmin" \
  --from-literal=secret_key="minioadmin" \
  --from-literal=bucket_name="invoices" \
  -n openfaas-fn --dry-run=client -o yaml | kubectl apply -f -
```

---

## 3. Compilation et Déploiement des Fonctions

Pour compiler les conteneurs de fonctions sous le tag Docker Hub et les pousser sur la passerelle OpenFaaS, utilisez le script `03-deploy.sh` :

```bash
# Définir votre nom d'utilisateur Docker Hub pour pousser les images
export DOCKER_HUB_USERNAME="votre_username_dockerhub"

# Lancer le build, le push et le deploy
./03-deploy.sh
```

*Note : Les images de fonctions configurées dans `stack.yml` utilisent la substitution de variable `${DOCKER_HUB_USERNAME}`.*

---

## 4. Guide de Validation des Scénarios Métier (Tests)

Une fois le déploiement terminé, validez les transitions d'états à l'aide des appels `curl` suivants.

### Scénario A : Succès Nominal du Workflow SAGA
Le client envoie un panier standard.
```bash
curl -i -X POST -H "Content-Type: application/json" \
  -d '{"cart_id": "cart_valide_99", "user_id": "client_jean", "total_amount": 149.50, "items": [{"name": "Livre OpenFaaS", "price": 149.50, "quantity": 1}]}' \
  http://127.0.0.1:8080/function/checkout-gateway
```
**Comportement attendu :**
- Réponse `201 Created` instantanée avec un ID de commande.
- En tâche de fond, la commande passe de `PENDING_PAYMENT` -> `PAID` (le simulateur valide).
- La facture est archivée sur MinIO sous `factures/YYYY-MM/invoice_[order_id].json`.
- Le statut final passe à `INVOICED` avec une URL de facture valide.
- Une alerte Discord avec un embed vert et cliquable apparaît sur votre canal de notification.

---

### Scénario B : Échec de Paiement par Solde Insuffisant (Compensé/FAILED)
Le client tente d'acheter un panier dont le montant total est supérieur ou égal à 1000.00 EUR (règle de limite de notre simulateur).
```bash
curl -i -X POST -H "Content-Type: application/json" \
  -d '{"cart_id": "cart_trop_cher", "user_id": "client_jean", "total_amount": 1200.00}' \
  http://127.0.0.1:8080/function/checkout-gateway
```
**Comportement attendu :**
- Réponse `201 Created` instantanée.
- Le simulateur rejette le paiement. Le statut passe à `FAILED`.
- Le workflow SAGA s'arrête (aucune facture générée, aucun dépôt S3 ni webhook).

---

### Scénario C : Échec de Paiement par détection de Carte Frauduleuse (FAILED)
Le client utilise un panier dont l'identifiant contient le mot `"fail"`.
```bash
curl -i -X POST -H "Content-Type: application/json" \
  -d '{"cart_id": "cart-fail-validation", "user_id": "client_jean", "total_amount": 50.00}' \
  http://127.0.0.1:8080/function/checkout-gateway
```
**Comportement attendu :**
- Réponse `201 Created`.
- Le paiement échoue explicitement. Le statut en base de données passe à `FAILED`.

---

### Scénario D : Lecture de l'Historique
Vous pouvez interroger à tout moment l'état des commandes via l'API synchrone :
```bash
# Récupérer toutes les commandes de "client_jean"
curl "http://127.0.0.1:8080/function/order-history-api?user_id=client_jean"

# Récupérer toutes les commandes passées au statut "FAILED"
curl "http://127.0.0.1:8080/function/order-history-api?status=FAILED"
```

---

## 5. Guide de Validation Visuelle (Correction Académique)

Voici comment prouver au correcteur académique le fonctionnement de la SAGA asynchrone et de l'auto-scaling.

### A. Preuve des Transitions d'États en DB
Connectez-vous directement au conteneur PostgreSQL pour voir la base de données évoluer en temps réel :
```bash
docker exec -it saga-postgres psql -U postgres -d saga_ecommerce -c "SELECT order_id, status, total_amount, invoice_url FROM orders ORDER BY created_at DESC;"
```
Vous verrez des lignes à l'état `INVOICED` avec leur lien MinIO pour les paniers à 149.50 €, et des lignes à l'état `FAILED` pour les paniers supérieurs à 1000 € ou contenant le mot `fail`.

### B. Preuve de l'Archivage S3 (MinIO)
1. Ouvrez l'interface graphique de la console MinIO dans votre navigateur : [http://localhost:9001](http://localhost:9001).
2. Connectez-vous avec les identifiants : `minioadmin` / `minioadmin`.
3. Cliquez sur **Object Browser** puis sélectionnez le bucket `invoices`.
4. Naviguez dans le dossier `factures/` pour télécharger et inspecter les fichiers JSON générés.

### C. Preuve de la saturation NATS & Pic d'Auto-Scaling
1. Ouvrez un terminal et surveillez les pods de la fonction lourde `invoice-archiver` :
   ```bash
   kubectl get pods -n openfaas-fn -w
   ```
2. Lancez le script de stress-test dans un autre terminal :
   ```bash
   ./04-load-test.sh
   ```
3. **Observation visuelle :**
   - Le script va envoyer des centaines de demandes en parallèle à `checkout-gateway`.
   - La file NATS interne à OpenFaaS va se remplir.
   - Les métriques de trafic interne de la gateway vont montrer la charge.
   - Grâce aux annotations configurées dans le `stack.yml` (`com.openfaas.scale.max: "10"`), OpenFaaS va immédiatement détecter le pic de requêtes sur la file et multiplier le nombre de réplicas de la fonction `invoice-archiver` de **1 à 10 pods** pour absorber le trafic.
   - Vous verrez les nouveaux pods `invoice-archiver-xxx` passer à l'état *Running* dans la sortie de votre terminal Kubernetes.
   - Une fois la file vidée, OpenFaaS réduira automatiquement le nombre de pods au minimum configuré (`scale.min: 1`).

### D. Preuve des Métriques Custom Prometheus
Chaque traitement de facture réussi incrémente un compteur custom nommé `saga_invoices_archived_total`. Pour prouver au correcteur que la métrique est correctement exposée au format standard de Prometheus :
1. Envoyez une requête HTTP GET sur le endpoint `/metrics` de la fonction d'archivage :
   ```bash
   curl http://127.0.0.1:8080/function/invoice-archiver/metrics
   ```
2. **Observation attendue :**
   Vous verrez s'afficher les lignes de métriques Prometheus brutes, confirmant que le compteur a bien été incrémenté :
   ```text
   # HELP saga_invoices_archived_total Total number of invoices successfully archived into S3/MinIO by the SAGA flow
   # TYPE saga_invoices_archived_total counter
   saga_invoices_archived_total 1.0
   ```

