"""D'où vient une connexion : `local`, `tailscale` ou `internet`. La seule définition, pour le serveur (le droit « Télécharger à distance ») et le registre d'activité (l'onglet État de l'Administration).

- `local` : la preuve d'être à la maison. Une connexion directe depuis une adresse du réseau privé, ou depuis la boucle locale SANS `X-Forwarded-For` (ce qui n'arrive pas par le proxy).
- `tailscale` : l'adresse réelle du client est dans la plage de Tailscale, qu'elle arrive en direct ou par le proxy de la boucle locale (`tailscale serve`). Un appareil du tailnet peut être n'importe où : il n'est pas à la maison.
- `internet` : tout le reste, adresse illisible comprise (Funnel, un client qui n'est pas du tailnet).

Le dernier mot d'un en-tête `X-Forwarded-For` n'est cru que d'une connexion de la boucle locale : l'appelant le donne (`forwarded`) et cette fonction l'ignore pour toute autre connexion. Derrière le proxy, rien n'est `local`, même un en-tête qui dit « 192.168.0.5 » : aucun en-tête ne prouve la maison.
"""

from __future__ import annotations

import ipaddress

TAILSCALE = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))
HOME = tuple(ipaddress.ip_network(net) for net in ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "::1/128", "fc00::/7", "fe80::/10"))
# Explicite, pas `is_private` : selon la version de Python, 100.64.0.0/10 et les plages de documentation (203.0.113.0/24…) y comptent ou non.


def parse(address):
    """L'adresse de `address` (un texte : une adresse, avec ou sans zone `%eth0`, ou un réseau /64 comme en rend `client_ip`), une IPv4 vue en double pile ramenée à IPv4 ; None si elle est illisible."""
    try:
        found = ipaddress.ip_network(str(address).partition("%")[0]).network_address
    except ValueError:
        return None
    return getattr(found, "ipv4_mapped", None) or found


def within(address, networks):
    return any(address.version == net.version and address in net for net in networks)


def origin_of(peer, forwarded=None):
    """`local`, `tailscale` ou `internet` pour la connexion venue de `peer`, avec `forwarded` : la dernière entrée de `X-Forwarded-For` (None si absente ; pas crue hors de la boucle locale). Voir l'en-tête du module."""
    real = parse(peer)
    if real is None:
        return "internet"
    if real.is_loopback:
        if forwarded is None:
            return "local"
        real = parse(forwarded)
        if real is None:
            return "internet"
        return "tailscale" if within(real, TAILSCALE) else "internet"
    if within(real, TAILSCALE):
        return "tailscale"
    return "local" if within(real, HOME) else "internet"
