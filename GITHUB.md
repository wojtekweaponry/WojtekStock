# Keeping Wojtek Stock on GitHub

Each version you deploy with `deploy.sh` is committed to git, labeled with its version (v8, v9…) and pushed to a
**private** GitHub repository, the same way as the Wojtek Locator. You get the whole history, can see what changed
between any two versions, and other people can contribute through pull requests without anyone's work being
overwritten.

**What never goes to GitHub:** your settings (`stock.env`: the WooCommerce key and secret, the shop password) and your
data (`data/`: the database). The `.gitignore` file keeps them out.

The examples use the repository `wojtekweaponry/WojtekStock`. If you name it differently, use your name instead.

## Once: setting it up (about 10 minutes, on the server)

**1. Make the repository on GitHub.** github.com → **+** → **New repository**. Name it `WojtekStock`, choose
**Private**, and *don't* add a README, .gitignore or license. Create it.

**2. Unpack v8 (or newer) first**, so the folder has `.gitignore` and `deploy.sh`:

```bash
cd ~ && unzip -o wojtek-stock-v8.zip
```

**3. Give the server its own key to this repository** (a "deploy key": it opens this one repository and nothing else in
your GitHub account):

```bash
ssh-keygen -t ed25519 -f ~/.ssh/github_wojtek_stock -N "" -C "wojtek-stock server"
cat ~/.ssh/github_wojtek_stock.pub
```

Copy the line it prints. On GitHub, in the WojtekStock repository: **Settings → Deploy keys → Add deploy key**. Paste
it, name it "wojtek-stock server", tick **Allow write access**, and add it.

**4. Tell SSH to use that key for this repository.** The Locator already has its own key for `github.com`, and a deploy
key only opens its own repository, so Wojtek Stock gets its own name for GitHub, `github-wojtek-stock`. This leaves the
Locator's setup exactly as it is.

```bash
cat >> ~/.ssh/config <<'END'

Host github-wojtek-stock
    HostName github.com
    User git
    IdentityFile ~/.ssh/github_wojtek_stock
    IdentitiesOnly yes
END
ssh -T git@github-wojtek-stock
```

The last line should answer "Hi wojtekweaponry/WojtekStock! You've successfully authenticated…" (and that GitHub
doesn't provide shell access, which is normal). The first time, it asks whether to trust GitHub's fingerprint: type
`yes`.

**5. Make the folder a git repository and look before sending anything:**

```bash
cd ~/wojtek-stock
git init -b main
git config user.name "wojtek-stock server"
git config user.email "server@localhost"
git remote add origin git@github-wojtek-stock:wojtekweaponry/WojtekStock.git
git add -A
git status
```

**Check the list `git status` shows.** It should be the program's files (`server.py`, `index.html`, `README.md`,
`deploy.sh`, `stock.env.example`, the `tests` folder…). It must **not** include `stock.env` or anything in `data/`.
If either shows up, stop and ask: a password or key that reaches GitHub stays in its history even if deleted later
(you'd change the key or password instead).

**6. Send the first version:**

```bash
git commit -m "v8: first version on GitHub"
git tag v8
git push -u origin main --tags
```

Refresh the repository on GitHub: the files are there, with the commit "v8: first version on GitHub".

**7. Try `deploy.sh` once.** It deploys the newest zip in your home folder (v8): unpacks it, finds nothing new to
commit, labels it, pushes, then rebuilds and restarts Wojtek Stock.

```bash
~/wojtek-stock/deploy.sh
```

It ends with "== v8 is running".

## From now on: each new version

On your Mac or PC (PowerShell: `$HOME\Downloads\wojtek-stock-v9.zip` instead):

```bash
scp ~/Downloads/wojtek-stock-v9.zip pepe@10.42.10.246:~/
ssh pepe@10.42.10.246
~/wojtek-stock/deploy.sh
```

`deploy.sh` unpacks it, commits it to git with the line in `CHANGES.txt` as its description, labels it `v9`, pushes it
to GitHub, then rebuilds and restarts. If GitHub can't be reached, Wojtek Stock still deploys, and it says so.

## When someone else contributes

1. **Add them to the repository:** GitHub → the repository → **Settings → Collaborators → Add people**. They fork it,
   make their change, and open a **pull request**.
2. **If you'd like Claude to look it over first:** on the pull request's page, add `.diff` to the end of the address
   (`…/pull/1.diff`), save that page, and upload it to the chat.
3. **Merge it** on GitHub: **Merge pull request → Confirm merge**.
4. **On the server:** `~/wojtek-stock/deploy.sh pull` takes what was merged, rebuilds and restarts.
5. **Before the next zip:** download the current code (the repository → **Code → Download ZIP**) and upload it to the
   chat, so the next version is built on top of the merged work.

`deploy.sh` protects merged work: it refuses a zip while GitHub has changes the server doesn't (run `deploy.sh pull`
first), and stops a zip that would change back files someone else edited. A zip that was built from the current GitHub
code may still need `~/wojtek-stock/deploy.sh --force ~/wojtek-stock-vNN.zip`; that's expected.

## If a push is ever refused

("the remote contains work that you do not have locally"). Wojtek Stock still deployed; GitHub just has commits the
server lacks. Look before doing anything:

```bash
cd ~/wojtek-stock && git fetch origin
git log --oneline HEAD..origin/main
```

and bring that list to the chat.
