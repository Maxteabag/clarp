# Teams as recursive groups

Product requirements: teams begin as a way to group work. Creating a team must
not enable agent behavior. These requirements supersede conflicting defaults in
the historical `teams-feature-plan.md`; the contract below is implemented by the Host and native client.

## Structure

- A team can contain agents and subteams.
- Every subteam is a normal team and can contain further subteams, with no
  product-imposed depth limit.
- Reject self-parenting and ancestor cycles.
- Grouping alone must not inject coordination instructions, share agent updates,
  wake agents, assign a leader, or trigger autonomous work.

## Team settings

- **Leader role:** disabled by default. Enabling it allows an eligible team
  member to be selected as leader. Disabling it stops leader-specific prompt
  instructions and autonomous leader activity.
- **Intra-team communication:** disabled by default. Enabling it permits the
  team's communication features. Disabling it stops capture/fanout and delivery
  of team updates, including previously queued updates.
- The settings are independent and belong to each team. A new subteam starts
  with both disabled; nesting does not implicitly enable either setting.
- Nesting alone does not route messages between parent, child, or sibling teams.

## Chats overview

- Show an agent's team hierarchy in the existing team chip as a compact tag.
  For Lena in Development beneath Clarp, display `Clarp › Development`.
- Combine the ancestor and subteam into one path instead of separate `Clarp`
  and `Development` chips for that same membership branch.
- Keep distinct membership branches distinguishable; do not flatten unrelated
  teams into a misleading parent/child path.
- For deeply nested teams, truncate the visual path to fit the chip while
  keeping the full hierarchy available through its accessible label and team
  detail view.

## Acceptance examples

1. Create a team and add agents: they remain behaviorally independent.
2. Add several levels of subteams: each can be managed like any other team.
3. Attempt to move a team beneath its descendant: reject without changing the
   existing hierarchy.
4. Enable communication in one team: unrelated and nested teams remain disabled.
5. Disable communication with unread updates queued: those updates are not
   injected into subsequent agent turns while disabled.
6. Enable and then disable a leader: no subsequent leader tick or leader prompt
   is admitted while the role is disabled.

## Storage and compatibility

- Migration preserves existing communication and configured leaders. New teams
  explicitly default to both settings off, including on upgraded databases.
- Existing leader nudging preferences are retained. New teams start with nudging
  off as well; it only runs when the leader role is enabled.
- Each team has at most one parent. Leaders must be direct members. Removing a
  leader from membership clears that role.
- Archiving or deleting a parent promotes its immediate children to root teams;
  their members, descendants, and history are preserved.
- The API exposes `parent_team_id`, `leader_enabled`, and
  `communication_enabled`. An omitted parent in updates leaves it unchanged;
  null or an empty string moves a team to the root.
- Intra-team broadcasts use `<team>` content, independently of spoken replies.
  Disabled communication blocks capture, inbox delivery, and team heartbeat
  wakeups. Turn retries refresh their team context before provider delivery.
- Coordinate the Host API and client settings/navigation changes. The iOS
  client lives in the separate `clarp-ios` repository.
