"""
ant_colony/colony/node_registry.py

NodeRegistry — centraal register van vertrouwde nodes.

De Queen is de enige autoriteit die nodes mag registreren en vertrouwen (P1).
Agents en adapters kennen de NodeRegistry niet — zij ontvangen een Mission
die al een gevalideerde allowed_node bevat.

Verantwoordelijkheden:
  - Bijhouden welke nodes vertrouwd en actief zijn
  - Node-status bijwerken (heartbeat stale → STALE, operator → SUSPENDED)
  - Valideren of een node een mission-aanvraag mag ontvangen

Regels:
  - register() overschrijft een bestaande node met dezelfde node_id met
    een waarschuwing (hot-swap, bijv. herconfiguratie)
  - get() retourneert None voor onbekende nodes — nooit een exception (P2)
  - unregister() is idempotent — onbekende node_id wordt genegeerd
  - is_trusted() is de primaire poort: True alleen als node bekend én ACTIVE
  - can_run_ant() en can_run_biome() zijn convenience-checks bovenop is_trusted()
  - Registry is bewust geen singleton — caller injecteert de instantie (P7)
  - Geen code wordt uitgevoerd bij import (P7)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from ant_colony.schemas.node import Node, NodeStatus

logger = logging.getLogger(__name__)


class NodeRegistry:
    """
    Centraal register van vertrouwde nodes.

    Usage::

        registry = NodeRegistry()
        registry.register(Node(
            node_id="pc2-desktop",
            hostname="DESKTOP",
            allowed_biomes=["crypto"],
            allowed_ant_types=["paper_ant", "research_ant"],
            heartbeat_interval=60,
            runtime_paths=RuntimePaths(output=..., live=..., logs=...),
        ))

        if registry.is_trusted("pc2-desktop"):
            # node is actief en mag missions ontvangen
            ...
    """

    def __init__(self) -> None:
        self._nodes: dict[str, Node] = {}

    # ------------------------------------------------------------------
    # Registratie
    # ------------------------------------------------------------------

    def register(self, node: Node) -> None:
        """
        Registreer een node.

        Overschrijft een bestaande node met dezelfde node_id met een
        waarschuwing. Dit maakt herconfiguratie mogelijk zonder herstart.

        Args:
            node: Volledig geconfigureerde Node. node_id mag niet leeg zijn.
        """
        if not node.node_id:
            raise ValueError("node.node_id must not be empty")

        if node.node_id in self._nodes:
            logger.warning(
                "NodeRegistry: overwriting existing node for node_id='%s'",
                node.node_id,
            )

        self._nodes[node.node_id] = node
        logger.debug(
            "NodeRegistry: registered node_id='%s' status=%s",
            node.node_id, node.status.value,
        )

    def unregister(self, node_id: str) -> None:
        """
        Verwijder een node uit het register.

        Idempotent: onbekende node_id wordt genegeerd zonder fout.
        """
        if node_id not in self._nodes:
            logger.debug(
                "NodeRegistry: unregister called for unknown node_id='%s' — ignored",
                node_id,
            )
            return
        del self._nodes[node_id]
        logger.debug("NodeRegistry: unregistered node_id='%s'", node_id)

    # ------------------------------------------------------------------
    # Opzoeken
    # ------------------------------------------------------------------

    def get(self, node_id: str) -> Node | None:
        """
        Zoek een node op node_id.

        Returns:
            Node als geregistreerd, anders None (fail-closed — nooit raises).
        """
        return self._nodes.get(node_id)

    def is_registered(self, node_id: str) -> bool:
        """True als er een node geregistreerd is voor node_id."""
        return node_id in self._nodes

    # ------------------------------------------------------------------
    # Trust-checks
    # ------------------------------------------------------------------

    def is_trusted(self, node_id: str) -> bool:
        """
        True als de node bekend én actief is (status == ACTIVE).

        Fail-closed: onbekende of niet-actieve nodes zijn nooit vertrouwd.
        Dit is de primaire poort die Queen gebruikt bij mission-validatie.
        """
        node = self._nodes.get(node_id)
        return node is not None and node.status == NodeStatus.ACTIVE

    def can_run_ant(self, node_id: str, ant_type: str) -> bool:
        """
        True als de node vertrouwd is én het ant_type in allowed_ant_types staat.

        Args:
            node_id:  Node-identifier.
            ant_type: Agent-type (bijv. "paper_ant", "research_ant").
        """
        node = self._nodes.get(node_id)
        if node is None or node.status != NodeStatus.ACTIVE:
            return False
        return ant_type in node.allowed_ant_types

    def can_run_biome(self, node_id: str, biome_id: str) -> bool:
        """
        True als de node vertrouwd is én het biome in allowed_biomes staat.

        Args:
            node_id:  Node-identifier.
            biome_id: Biome-identifier (bijv. "crypto", "equities").
        """
        node = self._nodes.get(node_id)
        if node is None or node.status != NodeStatus.ACTIVE:
            return False
        return biome_id in node.allowed_biomes

    # ------------------------------------------------------------------
    # Status mutaties
    # ------------------------------------------------------------------

    def set_status(self, node_id: str, status: NodeStatus) -> None:
        """
        Wijzig de status van een geregistreerde node.

        Idempotent: onbekende node_id wordt genegeerd.

        Args:
            node_id: Node-identifier.
            status:  Nieuwe NodeStatus.
        """
        node = self._nodes.get(node_id)
        if node is None:
            logger.warning(
                "NodeRegistry: set_status called for unknown node_id='%s' — ignored",
                node_id,
            )
            return
        self._nodes[node_id] = node.model_copy(update={"status": status})
        logger.debug(
            "NodeRegistry: node_id='%s' status → %s", node_id, status.value
        )

    def record_heartbeat(self, node_id: str) -> None:
        """
        Registreer een heartbeat voor node_id en zet status terug op ACTIVE.

        Idempotent: onbekende node_id wordt genegeerd.
        """
        node = self._nodes.get(node_id)
        if node is None:
            logger.warning(
                "NodeRegistry: record_heartbeat called for unknown node_id='%s' — ignored",
                node_id,
            )
            return
        self._nodes[node_id] = node.model_copy(update={
            "last_heartbeat": datetime.now(tz=timezone.utc),
            "status": NodeStatus.ACTIVE,
        })
        logger.debug("NodeRegistry: heartbeat recorded for node_id='%s'", node_id)

    # ------------------------------------------------------------------
    # Overzicht
    # ------------------------------------------------------------------

    def list_nodes(self) -> list[str]:
        """Gesorteerde lijst van geregistreerde node_ids."""
        return sorted(self._nodes.keys())

    def list_trusted(self) -> list[str]:
        """Gesorteerde lijst van node_ids met status ACTIVE."""
        return sorted(
            node_id
            for node_id, node in self._nodes.items()
            if node.status == NodeStatus.ACTIVE
        )

    @property
    def count(self) -> int:
        """Aantal geregistreerde nodes."""
        return len(self._nodes)
