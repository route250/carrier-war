# Quick integration test: create a session and call step with client-sent player_observation
import sys
from pprint import pprint
sys.path.insert(0, '.')
from server.services.session import store
from server.schemas import SessionCreateRequest, SessionStepRequest, PlayerObservation, SquadronLight

# Create session
req = SessionCreateRequest()
sresp = store.create(req)
print('created session', sresp.session_id)

sid = sresp.session_id
# Build a client-sent observation that is empty (likely mismatch with server calc)
client_obs = PlayerObservation(visible_squadrons=[])
step_req = SessionStepRequest(player_observation=client_obs)
resp = store.step(sid, step_req)
print('step done; logs:')
for l in resp.logs:
    print('-', l)

# Print returned player_intel to inspect server's computed observation
print('player_intel:', resp.player_intel)
print('turn_visible sample (len):', len(resp.turn_visible if resp.turn_visible else []))

# Print effects
print('effects:', resp.effects)

print('done')
