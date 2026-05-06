Phase 1 of TASK-FORGE-FRR-F010M is complete —        
  docs/research/forge-autobuild-runner-pipeline-emitter-bridge-scope.md is fully populated and ready for
  /feature-spec.
                                                                                                            

  Acceptance criteria status:

  - AC-1 (scoping doc with eight sections) — ✅ all sections populated.
  - AC-2 (FW10 audit) — ✅ definitive finding written: FW10-010's design is structurally broken by F010J's
  sidecar shape (the in-process emitter handle does not survive JSON serialization across the HTTP
  boundary), FW10-009 and F010F are sync-only, FW10-011 deliberately short-circuits over the sidecar. F010M
  remains real feature work.
  - AC-3 (≥4 candidate architectures) — ✅ six options (A polling, B webhooks, C streaming, D in-sidecar
  emit with HTTP/NATS sub-shapes, E hybrid, F runs.join).
  - AC-4 (cross-cutting concerns enumerated) — ✅ all seven from the F010M task body × six options, plus a
  summary matrix; #5 (forge status) and #7 (cancel) flagged as option-orthogonal.
  - AC-5/AC-6/AC-7 — pending Phases 2-4 (/feature-spec, /feature-plan, child-task filing).
  - AC-8 — explicitly deferred to wave-plan implementation.

  Recommended pick: Option C (Streaming via runs.join_stream + Last-Event-ID), with Option E (Hybrid) as
  named fallback. Highest-weight reason: C is the only option that captures every transition (per-stage AND
  terminal) with a structurally clean crash-recovery story. Dominant risk: SSE StreamPart → typed payload
  translation layer brittleness across langgraph-api version bumps; mitigation written into the doc.

  Key facts surfaced for /feature-spec:

  - langgraph-sdk==0.3.13 ships runs.create(webhook=...), runs.join_stream(last_event_id=...), runs.join,
  runs.cancel(action="interrupt"|"rollback") — all design-space options are buildable today, no version
  bumps needed.
  - Q3 (deferred-ack contract) is a wave-plan structural concern independent of bridge option choice — the
  consumer needs to hand off the ack callback to the bridge regardless.
  - Q4 (pause/resume canonicalisation) introduces a real double-publish risk between any bridge option and
  FW10-010's approval_subscriber.py resume site; F010M's wave-plan must pick one canonical site.

  What's next? Three options for the next session:

  - (a) Drive /feature-spec against this scope doc (Phase 2 — produces BDD scenarios under docs/design/).
  - (b) Pause and let you read the populated scope doc / push back on any of the calls (FW10-010 fold, the
  C-vs-E recommendation, Q4 canonicalisation choice).
  - (c) Commit the current state of the scope doc + task transition to in_progress, then resume in a
  separate session.

  Lean (b) before launching /feature-spec — the recommended option (C) and the Q4/Q8 ratification calls in
  §Recommended option are the ones /feature-spec will rest on, and a sanity check from you before committing
   to those is cheap and high-information.

✻ Baked for 4m 53s

❯ a                                                                                                         
                                         
● Let me check the precedent for where /feature-spec output lands, then invoke the skill.                   
                                               
  Listed 3 directories (ctrl+o to expand)                                                                   
                                                                                                            
● Let me verify the context files exist before invoking the skill.                                          
                                                                                                   
● Bash(for f in \                                                                                           
        docs/research/forge-autobuild-runner-pipeline-emitter-bridge-scope.md \…)         
  ⎿  OK: docs/research/forge-autobuild-runner-pipeline-emitter-bridge-scope.md                              
     OK: tasks/completed/TASK-FW10-009-validation-surface-and-build-failed-paths.md                         
     OK: tasks/completed/TASK-FW10-010-pause-resume-publish-round-trip.md                                   
     … +12 lines (ctrl+o to expand)                                                                         
                                                                                                            
● All context files verified. Invoking /feature-spec now.                                                   
                                                                                          
● Skill(/feature-spec)                                                                                      
  ⎿  Successfully loaded skill                                                                              
   
● Executing Phase 1 (Context Gathering) silently.                                                           
                                                                                          
  Read 1 file, listed 1 directory (ctrl+o to expand)                                                        
                                                                                                            
● Context loaded: stack=python, 10 existing .feature files (FEAT-FORGE-010 /
  forge-serve-orchestrator-wiring.feature is the closest precedent — same shape this feature scopes onto),  
  12 context files read (scope doc + 5 task files + 3 contracts + 2 ADRs + RESULTS Addendum 5), recommended 
  bridge option from scope doc is C (Streaming) with E (Hybrid) as fallback.                                
                                                                                                            
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━    
  FEATURE SPEC PROPOSAL: Wire the autobuild_runner sidecar lifecycle bridge into forge serve                
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━                                              
                                                                                                            
  @feat-forge-frr-f010m @forge-autobuild-runner-pipeline-emitter-bridge                                     
  Feature: Wire the autobuild_runner sidecar lifecycle bridge into forge serve                              
    As Rich, the sole operator of Forge                                                                     
    I want forge serve to render the full lifecycle envelope sequence on JetStream for                      
      every autobuild that runs in the langgraph-runner sidecar (success, failure, pause,                   
      resume, cancel) so the chat REPL can render between-prompt notifications for the                      
      actual progress of the build, instead of going silent the moment the dispatch                         
      chain returns HTTP 200.                                                                               
                                                                                                            
    Background:                                                                                             
      Given forge serve is running as the canonical daemon        
      And a JetStream PIPELINE stream is reachable                                                          
      And the eight pipeline lifecycle subjects are the published contract                                  
      And a langgraph-runner sidecar is reachable at the configured URL                                     
      And the inbound build-queued envelope carries an originating correlation identifier                   
      And the autobuild dispatch chain returns a real task identifier without raising                       
      And the autobuild runs to real lifecycle progressions inside the sidecar                              
                                                                                                            
  ━━ GROUP A: Key Examples (5 scenarios) ━━                                                                 
                                                                                                            
    # Why: The headline F010M gap — every state transition the autobuild reaches inside                     
    #      the sidecar must produce a wire-visible envelope (DDR-030 contract). Closes
    #      the silent-on-the-wire failure mode captured in RESULTS Addendum 5.                              
    @key-example @smoke                                                                                     
    Scenario: An autobuild that runs to completion in the sidecar produces the full lifecycle envelope      
  sequence on the wire                                                                                      
      Given a build-queued envelope is delivered for a feature                                              
      And the dispatch chain launches the autobuild in the sidecar                                          
      When the autobuild progresses through every stage and reaches a successful terminal state inside the
  sidecar                                                                                                   
      Then a build-started envelope should be published for that feature
      And one stage-complete envelope should be published per real stage transition in transition order     
      And a build-complete envelope should be published as the terminal envelope                            
      And no envelope should be missing from the canonical sequence                                         
                                                                                                            
    # Why: An async failure inside the sidecar must produce build-failed on the wire —                      
    #      today's F010M gap. F010F covers sync-raise; this scenario covers async-raise.                    
    @key-example @smoke @regression                                                                         
    Scenario: An autobuild that fails asynchronously inside the sidecar produces build-failed on the wire
      Given a build-queued envelope is delivered for a feature                                              
      And the dispatch chain launches the autobuild in the sidecar
      When the autobuild fails inside the sidecar after the dispatch chain has returned                     
      Then a build-started envelope should be published for that feature                                    
      And a build-failed envelope should be published as the terminal envelope
      And the build-failed envelope should carry a human-readable failure reason                            
                                                                                                            
    # Why: F010C correlation-id contract — every envelope must thread the inbound correlation_id            
    @key-example @regression                                                                                
    Scenario: Every envelope published for a sidecar autobuild threads the inbound correlation identifier   
      Given a build-queued envelope is delivered with a known correlation identifier                        
      When the autobuild runs to a terminal state inside the sidecar                                        
      Then every envelope published for that build should carry the same correlation identifier             
      And no envelope for that build should carry a different correlation identifier                        
                                                                                                            
    # Why: The supervisor must remain responsive while the autobuild runs in-sidecar —                      
    #      operator can ask "where's my build?" mid-flight and get an answer.                               
    @key-example                                                                                            
    Scenario: The supervisor remains responsive while the autobuild runs in the sidecar
      Given an autobuild is in-flight inside the sidecar                                                    
      When the operator queries the build's status before terminal
      Then the supervisor should answer with the build's current observed lifecycle                         
      And the supervisor's answer should not block until the autobuild completes                            
                                                                                                            
    # Why: F010F's sync-raise safety net stays valid; the bridge must not double-publish                    
    @key-example @regression                                                                                
    Scenario: A synchronous dispatch raise still uses F010F's safety-net publish, not the bridge            
      Given a build-queued envelope is delivered for a feature                                              
      When the dispatch chain raises synchronously before the autobuild launches inside the sidecar
      Then exactly one build-failed envelope should be published for that feature                           
      And no build-started or stage-complete envelope should be published for that feature                  
                                                                                                            
  ━━ GROUP B: Boundary Conditions (4 scenarios) ━━                                                          
                                                                                                            
    # Why: A minimal autobuild (one stage) still produces the canonical sequence                            
    @boundary
    Scenario: A single-stage autobuild produces a build-started, exactly one stage-complete, and a terminal 
  envelope                                                                                                  
      Given a build-queued envelope is delivered for a feature whose plan resolves to one stage
      When the autobuild reaches a successful terminal state                                                
      Then exactly one build-started envelope should be published 
      And exactly one stage-complete envelope should be published                                           
      And exactly one build-complete envelope should be published 
      And the order should be build-started, stage-complete, build-complete                                 
                                                                  
    # Why: Q2 — replay-via-Last-Event-ID restart shape. The bridge resumes after restart                    
    #      and emits the in-window envelopes that fired during downtime.
    # [ASSUMPTION: confidence=medium] (ASSUM-001) Restart per-stage tolerance: the wire                     
    #      replays in-window envelopes after restart, recovered from the sidecar's                          
    #      run-event buffer. Sub-option (b) from §Open questions Q2 of the scope doc.                       
    @boundary                                                                                               
    Scenario: A forge daemon restart during an in-flight autobuild replays missed envelopes after the daemon
   resumes                                                                                                  
      Given an autobuild is in-flight inside the sidecar          
      And forge daemon restarts during the in-flight period                                                 
      When forge daemon resumes and re-establishes the lifecycle bridge                                     
      Then any envelope that the sidecar produced during the restart window should be published after resume
      And the canonical sequence reaching the operator should be complete                                   
                                                                                                            
    # Why: Restart-window outside the buffer — fall back to terminal-only observability,                    
    #      still terminal-correct.                                                                          
    # [ASSUMPTION: confidence=medium] (ASSUM-002) langgraph-api buffer retention is                         
    #      finite; a restart window exceeding buffer retention loses per-stage envelopes                    
    #      irrecoverably, but terminal observability is preserved via the bridge's                          
    #      recovery sweep.                                                                                  
    @boundary @edge-case                                                                                    
    Scenario: A forge daemon restart longer than the bridge's replay buffer still produces a terminal       
  envelope                                                                                                  
      Given an autobuild is in-flight inside the sidecar          
      And forge daemon restarts for a period exceeding the lifecycle bridge's replay buffer                 
      When forge daemon resumes after the autobuild has reached a terminal state inside the sidecar         
      Then a terminal envelope should be published once forge daemon catches up                             
      And the bridge's recovery flow should ack the inbound build-queued envelope                           
                                                                                                            
    # Why: ADR-ARCH-014 — at most one in-flight build at a time. The bridge must not                        
    #      double-process if duplicate-detection lets a duplicate envelope through.                         
    @boundary @regression                                                                                   
    Scenario: Duplicate dispatch attempts for the same in-flight build do not produce duplicate envelopes
      Given a build is already in-flight inside the sidecar for a feature                                   
      When a second build-queued envelope arrives for the same feature with the same correlation identifier 
      Then the consumer should ack and skip the duplicate envelope                                          
      And no second build-started envelope should be published for that feature                             
                                                                  
  ━━ GROUP C: Negative Cases (4 scenarios) ━━                                                               
                                                                  
    # Why: ADR-ARCH-008 — SQLite is the source of truth; transient JetStream failures                       
    #      must not corrupt the build's recorded state.           
    @negative @regression                                                                                   
    Scenario: A NATS publish failure during the bridge's terminal envelope does not regress the recorded
  build state                                                                                               
      Given an autobuild has reached a successful terminal state inside the sidecar
      When the bridge attempts to publish build-complete and the JetStream publish fails                    
      Then the build's recorded state in SQLite should remain at the terminal state                         
      And the publish failure should be logged at WARNING                                                   
                                                                                                            
    # Why: Sidecar unreachable mid-build — the bridge must not tear down state; it must                     
    #      reconnect and resume from where it left off.           
    @negative @edge-case                                                                                    
    Scenario: A transient sidecar disconnection mid-build does not produce a spurious build-failed envelope 
      Given the lifecycle bridge is observing an in-flight autobuild                                        
      When the sidecar becomes briefly unreachable and then reachable again                                 
      Then the bridge should reconnect and resume observation                                               
      And no spurious build-failed envelope should be published before terminal                             
                                                                                                            
    # Why: Sidecar permanently unreachable — eventually the bridge must declare the                         
    #      build failed (operator-visible) rather than wait forever.                                        
    # [ASSUMPTION: confidence=low] (ASSUM-003) The bridge applies a bounded reconnect                       
    #      schedule before declaring the build failed; the exact bound (count, backoff                      
    #      shape) is deferred to /feature-plan.                                                             
    @negative @edge-case                                                                                    
    Scenario: The lifecycle bridge declares a build failed if the sidecar remains unreachable beyond the    
  reconnect schedule                                                                                        
      Given an autobuild is in-flight inside the sidecar          
      When the sidecar becomes unreachable and exceeds the bridge's reconnect schedule                      
      Then a build-failed envelope should be published with a sidecar-unreachable failure reason            
      And the build's recorded state in SQLite should reflect the terminal failure                          
                                                                                                            
    # Why: F010M scope — async failure is the headline gap; failure reason must be                          
    #      operator-actionable, not raw exception spam.           
    @negative @regression                                                                                   
    Scenario: A build-failed envelope from an async sidecar failure carries an operator-readable failure
  reason                                                                                                    
      Given an autobuild is in-flight inside the sidecar          
      When the autobuild fails inside the sidecar with an unhandled exception                               
      Then the published build-failed envelope's failure reason should name the exception class             
      And the failure reason should include the exception message                                           
                                                                                                            
  ━━ GROUP D: Edge Cases (7 scenarios) ━━                                                                   
                                                                                                            
    # Why: Q3 deferred-ack contract — the inbound build-queued envelope must be acked                       
    #      at terminal arrival, not on dispatch return; otherwise a redelivery storm
    #      (the symptom captured in RESULTS Addendum 5).                                                    
    # [ASSUMPTION: confidence=high] (ASSUM-004) The inbound build-queued envelope's ack                     
    #      is deferred from dispatch return to terminal arrival. Sub-option (b) from                        
    #      §Open questions Q3 of the scope doc; matches DDR-019's no-wedge-the-queue                        
    #      contract while closing the redelivery storm.                                                     
    @edge-case @regression                                                                                  
    Scenario: The inbound build-queued envelope is acked when the autobuild reaches a terminal state, not   
  when the dispatch chain returns                                                                           
      Given a build-queued envelope is delivered for a feature    
      And the dispatch chain returns successfully and the autobuild launches in the sidecar                 
      When the autobuild has not yet reached a terminal state                                               
      Then the inbound build-queued envelope should not be acked                                            
      When the autobuild reaches a terminal state inside the sidecar                                        
      Then the inbound build-queued envelope should be acked                                                
                                                                                                            
    # Why: Q4 pause/resume canonicalisation — the bridge owns both; the                                     
    #      approval-subscriber is amended to skip its own emit when the bridge is wired.                    
    # [ASSUMPTION: confidence=medium] (ASSUM-005) Pause/resume canonicalisation: the                        
    #      lifecycle bridge owns both build-paused and build-resumed emissions; the                         
    #      approval-subscriber's resume path is amended to skip its own emit when the                       
    #      bridge is wired. Sub-option (a) from §Open questions Q4 of the scope doc.                        
    @edge-case @regression                                                                                  
    Scenario: A mandatory-approval pause inside the sidecar produces exactly one build-paused envelope      
      Given an autobuild is in-flight inside the sidecar                                                    
      When the autobuild reaches a stage requiring mandatory approval
      Then exactly one build-paused envelope should be published for that feature                           
      And the build-paused envelope should carry the inbound correlation identifier
                                                                                                            
    @edge-case @regression                                        
    Scenario: An approval response for a paused build produces exactly one build-resumed envelope           
      Given an autobuild is paused awaiting approval                                                        
      When a matching approval response is received
      Then exactly one build-resumed envelope should be published for that feature                          
      And the autobuild should advance to the next stage          
                                                                                                            
    # Why: Q7 cancel-emit ownership — the bridge synthesises build-cancelled on                             
    #      observed sidecar-side terminal=interrupted.
    # [ASSUMPTION: confidence=medium] (ASSUM-006) Cancel-emit ownership: the bridge                         
    #      synthesises build-cancelled on observed sidecar terminal=interrupted; forge's                    
    #      cancel handler does not synthesise the envelope directly. Sub-option (b)                         
    #      from §Open questions Q7 of the scope doc.                                                        
    @edge-case                                                                                              
    Scenario: An operator cancellation in-flight produces a build-cancelled envelope after the sidecar      
  acknowledges interrupt                                                                                    
      Given an autobuild is in-flight inside the sidecar                                                    
      When the operator cancels the build                                                                   
      Then the sidecar should be requested to interrupt the run   
      And once the sidecar reaches a terminal=interrupted state, a build-cancelled envelope should be       
  published for that feature                                                                                
      And the build-cancelled envelope should carry the inbound correlation identifier                      
                                                                                                            
    # Why: Q6 forge status observability — the in-flight registry the bridge needs for                      
    #      recovery is the same artefact `forge status --in-flight` queries.
    # [ASSUMPTION: confidence=medium] (ASSUM-007) The forge status command grows an                         
    #      --in-flight surface in F010M's wave-plan, sourced from the same SQLite                           
    #      registry the bridge uses for recovery. Sub-option (a) from §Open questions                       
    #      Q6 of the scope doc.                                                                             
    @edge-case                                                                                              
    Scenario: forge status surfaces in-flight builds the bridge is currently observing                      
      Given an autobuild is in-flight inside the sidecar                                                    
      When the operator runs forge status with the in-flight surface                                        
      Then the in-flight build should appear with its feature, build identifier, and current observed       
  lifecycle                                                                                                 
                                                                  
    # Why: Multi-build recovery — daemon restart with N in-flight builds; each must be                      
    #      reconciled. ADR-ARCH-014 caps at one but defensive against future loosening.
    @edge-case                                                                                              
    Scenario: A forge daemon restart with multiple in-flight builds reconciles every build's bridge
      Given multiple autobuilds are in-flight inside the sidecar                                            
      When forge daemon restarts and resumes                      
      Then each in-flight build should be reconciled and observation resumed                                
                                                                                                            
    # Why: Q8 sidecar-aware integration test — the wave-plan ships a separate                               
    #      sidecar-aware test rather than amending FW10-011.                                                
    # [ASSUMPTION: confidence=medium] (ASSUM-008) F010M's wave-plan ships a separate                        
    #      sidecar-aware E2E integration test; FW10-011 remains as the in-process                           
    #      composition lock. Sub-option (a) from §Open questions Q8 of the scope doc.                       
    @edge-case @regression                                                                                  
    Scenario: The sidecar-aware integration test asserts the canonical lifecycle sequence against a real    
  sidecar spin-up                                                                                           
      Given a real langgraph-runner sidecar is started for the test                                         
      And forge serve is started against the real sidecar                                                   
      When a build-queued envelope is delivered through the real wiring                                     
      Then the canonical lifecycle sequence should be observable on the real wire                           
      And the test should remain deterministic across re-runs                                               
                                                                                                            
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━                                              
  Total: 20 scenarios across 4 groups                                                                       
  Inferred assumptions: 8 (will be resolved in Phase 5)                                                     
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━                                              
                                                                                                            
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━                                              
  CURATION: Review each group                                                                               
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━                                              
                                                                                                            
  For each group, you may:                                                                                  
                                                                                                            
    [A] Accept  — include all scenarios in this group as-is                                                 
    [R] Reject  — exclude all scenarios in this group
    [M] Modify  — accept but specify changes (provide inline)                                               
    [+] Add     — accept and add additional scenarios you describe                                          
    [?] Defer   — mark group for future specification; excluded from output
                                                                                                            
  Fast path: type "A A A A" to accept all four groups at once.                                              
                                                                                                            
  GROUP A — Key Examples (5 scenarios): _                                                                   
  GROUP B — Boundary Conditions (4 scenarios): _                  
  GROUP C — Negative Cases (4 scenarios): _                                                                 
  GROUP D — Edge Cases (7 scenarios): _
                                                                                                            
✻ Brewed for 4m 39s                                               
                                                                                                            
❯ A A A A                                                                                                   
                                                                                                            
● All four groups accepted.                                                                                 
                                                                                                            
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━                                              
  EDGE CASE EXPANSION (optional)                                                                            
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━                                              
                                                                                                            
  I've identified 6 additional scenarios covering security, concurrency,                                    
  data integrity, and integration boundaries:                                                               
                                                                                                            
  Security / data-integrity (1):                                                                            
    - The bridge rejects an in-sidecar emit whose correlation identifier
      does not match the registered build (defends F010C contract across                                    
      the process boundary; only relevant if Option D/E ratified)                                           
                                                                                                            
  Concurrency (2):                                                                                          
    - The bridge's terminal observation races a synchronous F010F-style                                     
      raise — exactly one build-failed envelope is published (regression                                    
      test for the F010F + F010M boundary)                        
    - Two operator-cancel requests for the same in-flight build —                                           
      idempotent first-wins; exactly one build-cancelled envelope
                                                                                                            
  Data integrity / recovery (1):                                  
    - build-started has already been published, daemon crashes, recovery                                    
      resumes — exactly one build-started observable end-to-end (no                                         
      duplicate after resume)                                                                               
                                                                                                            
  Integration boundaries (2):                                                                               
    - The sidecar returns a malformed run-state response — bridge logs
      and reconnects rather than crashing the daemon
    - langgraph-api version mismatch detected at startup — fail fast                                        
      with a diagnostic naming the version skew
                                                                                                            
  Include them?                                                   
                                                                                                            
  [Y] Yes — show and curate them (same A/R/M/+/? actions)                                                   
  [S] Sample — show 2 examples, then decide
  [N] No — skip and proceed to Phase 5                                                                      
                                                                  
  Your choice [Y/S/N]:                                                                                      
                                                                  
✻ Churned for 27s                                                                                           
                                                                  
❯ Y                                                                                                         
    
● ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━                                              
  EDGE CASE SCENARIOS (6 additional)                              
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━                                              
                                                                                                            
    # Why: F010C correlation-id contract crosses the process boundary if Option D/E                         
    #      is ratified. The in-forge receiver must reject emits whose correlation_id                        
    #      doesn't match the registered build, defending against in-sidecar bugs                            
    #      that drop or corrupt the threading.                    
    # [ASSUMPTION: confidence=low] (ASSUM-009) Cross-process correlation-id                                 
    #      enforcement only relevant if /feature-spec ratifies Option D or E. Under
    #      Option C (recommended), the bridge runs in-forge and threading is trivial,                       
    #      making this scenario a no-op.                          
    @edge-case @security @regression                                                                        
    Scenario: An in-sidecar emit carrying a correlation identifier that does not match the registered build
  is rejected                                                                                               
      Given an autobuild is registered with a known correlation identifier
      When the in-sidecar bridge sends an emit carrying a different correlation identifier                  
      Then the receiving endpoint should drop the emit                                                      
      And no envelope should be published for the mismatched identifier
      And the rejection should be logged at WARNING                                                         
                                                                  
    # Why: F010F + F010M boundary regression — sync-raise and async-terminal must                           
    #      not double-publish if they happen close together (e.g. the bridge has
    #      already observed terminal but a synchronous handler-side raise fires                             
    #      simultaneously due to an unrelated bug).                                                         
    @edge-case @regression
    Scenario: A synchronous dispatch raise concurrent with the bridge's terminal observation produces       
  exactly one build-failed envelope                                                                         
      Given the bridge is observing an in-flight autobuild that has just reached a terminal failure inside
  the sidecar                                                                                               
      When a synchronous dispatch-time raise fires for the same feature at the same time
      Then exactly one build-failed envelope should be published for that feature                           
      And no second build-failed envelope should be published for that feature
                                                                                                            
    # Why: Idempotent first-wins (FEAT-FORGE-004 contract) extended to the cancel                           
    #      path — two cancel requests racing must not produce two build-cancelled
    #      envelopes.                                                                                       
    @edge-case @regression                                                                                  
    Scenario: Two operator cancellation requests for the same in-flight build produce exactly one
  build-cancelled envelope                                                                                  
      Given an autobuild is in-flight inside the sidecar          
      When two cancellation requests are issued for the same feature concurrently                           
      Then exactly one build-cancelled envelope should be published for that feature
      And the second cancellation request should be a no-op                                                 
                                                                                                            
    # Why: Recovery shape — if build-started has already been observed and published
    #      before a daemon restart, the post-restart bridge must not re-publish it                          
    #      after re-attaching to the run.                         
    @edge-case @regression                                                                                  
    Scenario: A daemon restart after build-started has been published does not re-publish build-started
  after recovery                                                                                            
      Given an autobuild has progressed past the build-started transition
      And a build-started envelope has already been published for that feature                              
      When forge daemon restarts and the lifecycle bridge reconciles the in-flight build
      Then no second build-started envelope should be published for that feature                            
      And subsequent envelopes should resume from where the bridge left off                                 
   
    # Why: Defensive — the langgraph-runner SDK contract may evolve; a response that                        
    #      doesn't parse must not crash the daemon. Logs at WARNING and reconnects.
    @edge-case                                                                                              
    Scenario: A malformed run-state response from the sidecar is logged and the bridge reconnects rather
  than crashing the daemon                                                                                  
      Given the lifecycle bridge is observing an in-flight autobuild
      When the sidecar returns a malformed run-state response                                               
      Then the bridge should log the parse failure at WARNING                                               
      And the bridge should reconnect to the sidecar
      And forge daemon should remain running                                                                
                                                                  
    # Why: SDK volatility risk (the dominant risk on Option C per scope doc                                 
    #      §Recommended option) — surface a langgraph-api version mismatch loudly
    #      at startup rather than silently emit malformed envelopes at runtime.                             
    # [ASSUMPTION: confidence=medium] (ASSUM-010) The bridge declares an expected                           
    #      langgraph-api / langgraph-sdk version range at startup and fails fast
    #      with a diagnostic naming the version skew if the running sidecar reports                         
    #      an out-of-range version.                               
    @edge-case @regression                                                                                  
    Scenario: A langgraph-runner version mismatch is detected at forge startup and fails the daemon with a  
  diagnostic
      Given forge serve is starting against a sidecar reporting a langgraph-api version outside the bridge's
   declared support range                                                                                   
      When forge daemon attempts to wire the lifecycle bridge
      Then forge daemon should fail to start                                                                
      And the diagnostic should name both the expected version range and the observed sidecar version
                                                                                                            
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━                                              
  CURATION: Review the edge case batch                                                                      
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━                                              
                                                                                                            
    [A] Accept all 6                                                                                        
    [R] Reject all 6                                                                                        
    [M] Modify (specify changes inline)                                                                     
    [+] Add additional scenarios                                                                            
    [?] Defer (exclude from output, file as follow-up)
                                                                                                            
  Per-scenario actions also accepted (e.g. "1:A 2:A 3:R 4:A 5:M ... 6:A").                                  
                                                                                                            
  Your choice: _                                                                                            
                                                                  
✻ Sautéed for 41s                
                                                                                          
❯ A                                                                                                         
                                                                                                            
● All 6 edge case scenarios accepted. Moving to Phase 5.                                                    
                                                                                                            
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━                                              
  ASSUMPTION RESOLUTION (10 items)                                                                          
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━                                              
                                                                                                            
  Each assumption has a proposed default. Press Enter to accept,                                            
  or type the actual value / desired override.                                                              
                                                                                                            
  [1] ASSUM-001 — Restart per-stage tolerance                     
      Proposed: replay missed envelopes after restart from the sidecar's run-event buffer                   
      Confidence: medium                                                                                    
      Basis: Scope doc §Open questions Q2 sub-option (b); §Recommended option lists this                    
             as the highest-weight reason for picking Option C (Last-Event-ID semantics)                    
      Accept or enter value: _                                                                              
                                                                                                            
  [2] ASSUM-002 — Replay buffer behaviour past retention                                                    
      Proposed: terminal observability preserved via recovery sweep; per-stage envelopes
                outside the buffer window are irrecoverable                                                 
      Confidence: medium                                          
      Basis: langgraph-api 0.8.5 server-side buffer is finite (default retention TBD);                      
             recovery sweep reconciles via runs.list / runs.get on startup                                  
      Accept or enter value: _                                                                              
                                                                                                            
  [3] ASSUM-003 — Reconnect schedule bound                                                                  
      Proposed: bounded reconnect schedule before declaring sidecar-unreachable; exact
                count and backoff shape deferred to /feature-plan                                           
      Confidence: low                                             
      Basis: Scope doc §Cross-cutting concern #6 names "explicit reconnect-with-backoff"                    
             as the C-shape; no concrete numbers chosen yet                                                 
      Accept or enter value: _                                                                              
                                                                                                            
  [4] ASSUM-004 — Deferred-ack contract                                                                     
      Proposed: inbound build-queued envelope is acked when the autobuild reaches a
                terminal state, not when the dispatch chain returns                                         
      Confidence: high
      Basis: Scope doc §Open questions Q3 sub-option (b); closes the redelivery storm                       
             captured in RESULTS Addendum 5; matches DDR-019's no-wedge-the-queue contract                  
      Accept or enter value: _                                                                              
                                                                                                            
  [5] ASSUM-005 — Pause/resume canonicalisation                                                             
      Proposed: lifecycle bridge owns both build-paused and build-resumed; the
                approval-subscriber's resume path is amended to skip its own emit when                      
                the bridge is wired                                                                         
      Confidence: medium                                                                                    
      Basis: Scope doc §Open questions Q4 sub-option (a); folds FW10-010 into F010M's                       
             wave-plan rather than duplicating the resume emit site                                         
      Accept or enter value: _                                                                              
                                                                                                            
  [6] ASSUM-006 — Cancel-emit ownership                                                                     
      Proposed: bridge synthesises build-cancelled on observed sidecar terminal=interrupted;                
                forge's cancel handler does not synthesise the envelope directly                            
      Confidence: medium                                                                                    
      Basis: Scope doc §Open questions Q7 sub-option (b); unifies "all terminal emits flow                  
             through the bridge"                                                                            
      Accept or enter value: _                 
                                                                                                            
  [7] ASSUM-007 — forge status --in-flight scope
      Proposed: in scope for F010M's wave-plan, sourced from the same SQLite registry
                the bridge uses for recovery                                                                
      Confidence: medium                                                                                    
      Basis: Scope doc §Open questions Q6 sub-option (a) (recommended); the in-flight                       
             registry is shared infrastructure                                                              
      Accept or enter value: _                 
                                                                                                            
  [8] ASSUM-008 — Sidecar-aware integration test scope                                                      
      Proposed: F010M's wave-plan ships a separate sidecar-aware E2E integration test;
                FW10-011 remains as the in-process composition lock                                         
      Confidence: medium                                                                                    
      Basis: Scope doc §Open questions Q8 sub-option (a); preserves FW10-011's existing                     
             design while closing the production-shape coverage gap                                         
      Accept or enter value: _                                                                              
                                                                                                            
  [9] ASSUM-009 — Cross-process correlation-id enforcement                                                  
      Proposed: only relevant if /feature-spec ratifies Option D or E. Under Option C
                (recommended), the bridge runs in-forge and F010C's existing AST lint                       
                guards extend trivially; the security scenario becomes a no-op test that                    
                still locks the contract should the option choice flip later                                
      Confidence: low                                                                                       
      Basis: Scope doc §Cross-cutting concern #4; option-discriminating assumption                          
      Accept or enter value: _                                                                              
                                               
  [10] ASSUM-010 — langgraph-api version-range fail-fast                                                    
       Proposed: bridge declares an expected langgraph-api / langgraph-sdk version range
                 at startup and fails fast with a diagnostic naming the version skew if                     
                 the running sidecar reports an out-of-range version
       Confidence: medium                                                                                   
       Basis: Scope doc §Recommended option dominant-risk mitigation; locks the SDK
              volatility risk loudly rather than silently emitting malformed envelopes                      
       Accept or enter value: _                
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━                                              
                                                                                                            
  Fast path: type "all" to accept every default, or "1 2 4 7 10" to accept just                             
  those numbered items and prompt me for overrides on the rest.                                             
                                                                                                            
  Your choice: _                               
                                                                                                            
✻ Sautéed for 38s                                                                                           
                                                                                          
❯ accept defaults                                                                                           
  ⎿  Not logged in · Please run /login                                                                      
                                                                                                            
✻ Sautéed for 0s                                                                                            
                                                                                                            
❯ /login                                                                                                    
  ⎿  Login successful
                                                                                                            
❯ continue                                                                                                  
                                                                                  
● All defaults accepted. Writing files.                                                                     
                                                                                                          
● Bash(mkdir -p features/forge-autobuild-runner-pipeline-emitter-bridge && ls                               
      features/forge-autobuild-runner-pipeline-emitter-bridge/)                                             
  ⎿  (No output)                                                                                            
                                                                                                            
● Write(features/forge-autobuild-runner-pipeline-emitter-bridge/forge-autobuild-runner-pipeline-emitter-brid
       ge.feature)                                                                                          
  ⎿  Wrote 331 lines to features/forge-autobuild-runner-pipeline-emitter-bridge/forge-autobuild-runner-     
     pipeline-emitter-bridge.feature                                                                        
       2 # Feature: Wire the autobuild_runner sidecar lifecycle bridge into forge serve (TASK-FORGE-F       
         RR-F010M)                                                                                          
       3 # Stack: python                                                                                    
       4 # Assumptions: 10 (see forge-autobuild-runner-pipeline-emitter-bridge_assumptions.yaml)            
       5 # Generated: 2026-05-06T00:00:00Z                                                                  
       6                                                          
       7 @feat-forge-frr-f010m @forge-autobuild-runner-pipeline-emitter-bridge
       8 Feature: Wire the autobuild_runner sidecar lifecycle bridge into forge serve
       9   As Rich, the sole operator of Forge
      10   I want forge serve to render the full lifecycle envelope sequence on JetStream for
     … +355 lines (ctrl+o to expand)

● Write(features/forge-autobuild-runner-pipeline-emitter-bridge/forge-autobuild-runner-pipeline-emitter-brid
       ge_assumptions.yaml)                                                               
  ⎿  Wrote 79 lines to features/forge-autobuild-runner-pipeline-emitter-bridge/forge-autobuild-runner-p
     ipeline-emitter-bridge_assumptions.yaml                                                                
       1 # Assumptions manifest for forge-autobuild-runner-pipeline-emitter-bridge.feature
       2 # Generated by /feature-spec                                                                       
       3 # Anchors TASK-FORGE-FRR-F010M Phase 2 deliverable                                                 
       4 
       5 feature: "Wire the autobuild_runner sidecar lifecycle bridge into forge serve"                     
       6 generated: "2026-05-06T00:00:00Z"                        
       7 stack: "python"
       8 review_required: true
       9
      10 assumptions:
     … +101 lines (ctrl+o to expand)

● Write(features/forge-autobuild-runner-pipeline-emitter-bridge/forge-autobuild-runner-pipeline-emitter-brid
       ge_summary.md)                                                                     
  ⎿  Wrote 102 lines to features/forge-autobuild-runner-pipeline-emitter-bridge/forge-autobuild-runner-
     pipeline-emitter-bridge_summary.md                                                                     
      1 # Feature Spec Summary: Wire the autobuild_runner sidecar lifecycle bridge into forge serve
      2                                                                                                     
      3 **Stack**: python                                                                                   
      4 **Generated**: 2026-05-06T00:00:00Z
      5 **Anchor task**: TASK-FORGE-FRR-F010M (scoping deliverable; this spec is its Phase 2 output)        
      6 **Anchor scoping doc**: `docs/research/forge-autobuild-runner-pipeline-emitter-bridge-scope.m
        d`
      7 **Scenarios**: 26 total (2 smoke, 14 regression)
      8 **Assumptions**: 10 total (1 high / 7 medium / 2 low confidence)
      9 **Review required**: Yes — 2 low-confidence assumptions need verification
     … +100 lines (ctrl+o to expand)

● ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  FEATURE SPEC COMPLETE: Wire the autobuild_runner sidecar lifecycle bridge into forge serve
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━                                              
                                                                                                            
  Files written:                                                                                            
    features/forge-autobuild-runner-pipeline-emitter-bridge/forge-autobuild-runner-pipeline-emitter-bridge.f
  eature                                                                                                    
    features/forge-autobuild-runner-pipeline-emitter-bridge/forge-autobuild-runner-pipeline-emitter-bridge_a
  ssumptions.yaml                                                                                           
    features/forge-autobuild-runner-pipeline-emitter-bridge/forge-autobuild-runner-pipeline-emitter-bridge_s
  ummary.md                                                                                                 
                                                                  
  Scenarios: 26 total                                                                                       
    @key-example: 5    @boundary: 4                               
    @negative: 4       @edge-case: 17
    @smoke: 2          @regression: 14
                                                                                                            
  Assumptions: 10 total
    high: 1 (auto-proceed) — ASSUM-004 (deferred-ack)                                                       
    medium: 7 (Coach review recommended) — ASSUM-001/002/005/006/007/008/010                                
    low: 2 (human review required) — ASSUM-003 (reconnect schedule numbers),                                
                                     ASSUM-009 (cross-process correlation-id, option-conditional)           
                                                                                                            
  REVIEW REQUIRED: 2 low-confidence assumptions need verification                                           
    See: features/forge-autobuild-runner-pipeline-emitter-bridge/forge-autobuild-runner-pipeline-emitter-bri
  dge_assumptions.yaml                                                                                      
                                                                  
  Deferred: 0 scenario groups (all four groups + edge-case expansion accepted)                              
                                                                  
  Next steps:                                                                                               
    Review: features/forge-autobuild-runner-pipeline-emitter-bridge/forge-autobuild-runner-pipeline-emitter-
  bridge.feature                                                                                            
    Pass to feature-plan: /feature-plan "Wire the autobuild_runner sidecar lifecycle bridge into forge
  serve" \                                                                                                  
      --context features/forge-autobuild-runner-pipeline-emitter-bridge/forge-autobuild-runner-pipeline-emit
  ter-bridge_summary.md                                                                                     
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━    
                                                                                                            
  /feature-spec complete. Phase 2 of TASK-FORGE-FRR-F010M is done — AC-5 satisfied (BDD scenarios saved     
  under features/forge-autobuild-runner-pipeline-emitter-bridge/, matching the FEAT-FORGE-010 precedent     
  shape).                                                                                                   
                                                                  
  Headline outcomes:                                                                                        
  - 26 scenarios (Group A 5 / B 4 / C 4 / D 7 / expansion 6) covering every cross-cutting concern from the
  scope doc                                                                                                 
  - 10 assumptions resolved; only 2 are low-confidence and both are well-bounded for /feature-plan to pick
  numbers/conditions                                                                                        
  - The spec is option-agnostic on the surface (all scenarios pass under C, E, or D) but                    
  option-discriminating in its assumptions (ASSUM-005, ASSUM-006, ASSUM-007, ASSUM-008, ASSUM-009 each
  commit to a sub-option from the scope doc's §Open questions)                                              
                                                                  
  Two options for the next move:                                                                            
                                                                                                            
  - (a) Continue to Phase 3 — /feature-plan in this same session. It'll consume the summary + scope doc +   
  supporting context to produce the wave-plan tasks under                                                   
  tasks/backlog/forge-autobuild-runner-pipeline-emitter-bridge/ (AC-6 + AC-7).                              
  - (b) Pause here. Phase 2 is a natural commit point — the scope doc + spec + assumptions form a
  self-contained deliverable. You can review the 26 scenarios + the 10 assumption resolutions before Phase 3
   commits to a wave-plan structure.
                                                                                                            
  Lean (b) for the same reason I leaned (b) before Phase 2: /feature-plan will cement the wave-plan         
  structure (file list, task ordering, complexity sizing) on top of these spec choices, and a sanity check
  from you on the option-discriminating assumptions before they harden into tasks is high-information and   
  cheap. Want me to commit the artefacts now (scope doc + feature folder + the task move from backlog →
  in_progress)?

✻ Brewed for 3m 56s                 
                                                                                          
❯ b                                                                                                         
                                                                                                            
● Pausing here. Phase 2 deliverable is on disk; Phase 3 deferred to a separate session.                     
                                                                                                            
  Status of the working tree (uncommitted):                                                                 
   
  - tasks/backlog/feat-jarvis-internal-001-followups/TASK-FORGE-FRR-F010M-…md →                             
  tasks/in_progress/feat-jarvis-internal-001-followups/… (state transition)
  - docs/research/forge-autobuild-runner-pipeline-emitter-bridge-scope.md (Phase 1 deliverable — eight      
  populated sections)                                                                                       
  - features/forge-autobuild-runner-pipeline-emitter-bridge/ (Phase 2 deliverable — 3 files: .feature,
  _assumptions.yaml, _summary.md)                                                                           
                                                                  
  How would you like to handle the changes?                                                                 
                                                                  
  - (i) Commit now with a single conventional commit (something like docs(F010M): scope doc + feature spec  
  for autobuild_runner ↔ pipeline-emitter bridge (Phase 1+2)).
  - (ii) Two commits — Phase 1 (scope doc + state transition) and Phase 2 (feature folder) separately, so   
  the history matches the F010M task's phased structure.                                                    
  - (iii) Leave the tree dirty for you to review the scope doc + 26 scenarios + 10 assumption resolutions
  first.                                                                                                    
                                                                  
  Lean (iii) — the spec choices encode option-discriminating assumptions (ASSUM-005/006/007/008/009) that   
  lock the wave-plan structure, so a read-through before committing is cheap and high-information. The
  feature folder is purely additive and the scope doc is in docs/research/, so leaving it dirty is safe.    
                                                                  
✻ Baked for 17s                       

❯ i                                                                                                         
                             
● Bash(git status)                                                                                          
  ⎿  On branch main                                               
     Your branch is ahead of 'origin/main' by 4 commits.                                                    
       (use "git push" to publish your local commits)
     … +16 lines (ctrl+o to expand)                                                                         
                                                                                                            
● Bash(git log --oneline -10)                    
  ⎿  378ccd6 fix(autobuild): retarget autobuild_runner LLM from anthropic to local llama-swap qwen36-wo     
     rkhorse (TASK-FORGE-FRR-F010L)                                                                         
     0e93026 reviews and tasks                   
     … +10 lines (ctrl+o to expand)                                                                         
                                                                                                            
● Bash(git diff --stat HEAD)                     
  ⎿   docs/history/command-history.md                    |   80 ++                                          
      ...tobuild-runner-pipeline-emitter-bridge-scope.md | 1081 ++++++++++++++++++--                        
      ...ope-autobuild-runner-pipeline-emitter-bridge.md |    0
      3 files changed, 1051 insertions(+), 110 deletions(-)                                                 
                                                                                                            
● docs/history/command-history.md was already dirty at session start and is unrelated to F010M — I'll
  exclude it from this commit. Staging only the F010M deliverables.                                         
                                                                                                            
● Bash(git add docs/research/forge-autobuild-runner-pipeline-emitter-bridge-scope.md
      features/forge-autobuild-runner-pipeline-emitter-bridge/ && git status)                               
  ⎿  On branch main                                                                                         
     Your branch is ahead of 'origin/main' by 4 commits.
       (use "git push" to publish your local commits)                                                       
     … +18 lines (ctrl+o to expand)                               
                                                                                                            
● Bash(git commit -m "$(cat <<'EOF'                
      docs(F010M): scope autobuild_runner ↔ pipeline-emitter bridge (Phase 1+2)…)                           
  ⎿  [main 816195a] docs(F010M): scope autobuild_runner ↔ pipeline-emitter bridge (Phase 1+2)               
      5 files changed, 1483 insertions(+), 110 deletions(-)                                                 
      create mode 100644 features/forge-autobuild-runner-pipeline-emitter-bridge/forge-autobuild-runner     
     … +17 lines (ctrl+o to expand)                                                                         
                                                                  
● Committed 816195a — 5 files / 1,483 insertions / 110 deletions. Branch is 5 commits ahead of origin/main. 
  docs/history/command-history.md left dirty (pre-existing, unrelated).                   
                                                                                                            
  Phase 1+2 of TASK-FORGE-FRR-F010M land cleanly; Phase 3 (/feature-plan) and Phase 4 (file wave-plan tasks 
  with parent_task: TASK-FORGE-FRR-F010M) deferred to a separate session.
                                                                                                            
✻ Cogitated for 45s                                               
                                                                                          
※ recap: Scoping the forge autobuild_runner ↔ pipeline-emitter bridge under TASK-FORGE-FRR-F010M; Phases 1  
  and 2 (scope doc plus 26 BDD scenarios) are committed as 816195a. Next: drive /feature-plan to produce the
   wave-plan tasks.                          