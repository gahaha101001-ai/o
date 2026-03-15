# FastAPI Base

Deployment-ready FastAPI app with:

- MongoDB-backed submissions and admin settings
- Redis-backed online visitor tracking
- Admin dashboard for reviewing submissions

## Run locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload
```

## Required services

- MongoDB
- Redis

## Render deployment

This repo now includes [`render.yaml`](/Users/m/Desktop/fastapi-base/render.yaml) for a Render Blueprint deploy.

What it provisions:

- one Render web service for FastAPI
- one Render Key Value instance for Redis

What you still need to provide in Render:

- `MONGO_URI`
  Render does not provide managed MongoDB in this blueprint, so use MongoDB Atlas or another external MongoDB provider.
- `ADMIN_USERNAME`
- `ADMIN_PASSWORD`
- `ALLOWED_HOSTS`
  Use your Render hostname first, then add your custom domain later.
- `CORS_ORIGINS`
  Leave blank if the app is served from the same origin.

Recommended first deploy values:

- `ALLOWED_HOSTS=your-service-name.onrender.com`
- `CORS_ORIGINS=`

Deploy flow:

1. Push this repo to GitHub or Bitbucket.
2. In Render, create a new Blueprint and select the repo.
3. Fill in the unsynced environment variables.
4. Set `MONGO_URI` to your external MongoDB connection string.
5. Deploy.

## Production environment

Copy [`.env.example`](/Users/m/Desktop/fastapi-base/.env.example) to `.env` and set:

- `ENV=production`
- `ADMIN_PASSWORD` to a strong password
- `ADMIN_SESSION_SECRET` to a long random secret
- `ALLOWED_HOSTS` to your real domain names
- `CORS_ORIGINS` only if the frontend is served from a different origin
- `REDIS_URL` and `MONGO_URI` to production services

The app now rejects production startup when:

- `ADMIN_SESSION_SECRET` is still a placeholder
- `ADMIN_PASSWORD` is still a weak default
- `ALLOWED_HOSTS` is empty or includes `*`

## Procfile platforms

The included [`Procfile`](/Users/m/Desktop/fastapi-base/Procfile) works for platforms that expect a `web` command.
