Imagine one neighbourhood gets heavy rain.

Normally, this happens:

```
🌧️ Heavy Rain
     │
     ▼
🌊 River rises
     │
     ▼
Someone notices
     │
     ▼
Someone calls officials
     │
     ▼
Officials send warning
     │
     ▼
People start calling for help
     │
     ▼
Someone manually finds volunteers
     │
     ▼
Someone checks shelters
     │
     ▼
Someone updates everyone
```

The problem?

**Everything is fragmented and manual.**

During a real emergency, people are asking:

> "Is my area affected?"
> 

> "Where should I go?"
> 

> "My family is trapped."
> 

> "Who can rescue us?"
> 

> "Which shelter has space?"
> 

> "Is the water safe?"
> 

And responders are asking:

> "Where is the emergency?"
> 

> "Who already responded?"
> 

> "Which shelter still has capacity?"
> 

# 🤖 2. Our idea

We create a **Neighbourhood Emergency Response Agent**.

Think of it like having a **24×7 digital emergency coordination team**.

```
                         🏘️ NEIGHBOURHOOD
                                │
                ┌───────────────┼───────────────┐
                │               │               │
                ▼               ▼               ▼
             🌧️ Weather      🌊 River        👤 People
             data            sensor/API       messages
                │               │               │
                └───────────────┼───────────────┘
                                ▼
                     🤖 AI EMERGENCY TEAM
                                │
             ┌──────────────────┼──────────────────┐
             │                  │                  │
             ▼                  ▼                  ▼
        👀 Monitor          🚨 Alert            📥 Intake
          Agent              Agent                Agent
             │                  │                  │
             │                  │                  ▼
             │                  │              🆘 Request
             │                  │                  │
             │                  │                  ▼
             │                  │             🚑 Dispatch
             │                  │                Agent
             │                  │                  │
             │                  │                  ▼
             │                  │             👨‍🚒 Responder
             │                  │
             └──────────────────┼────────────────────
                                ▼
                         🧠 Shared State
                          / Registry
                                │
                                ▼
                         🛡️ Safety / QA
```

But now comes the important part.

# 🧩 3. Where does Strands Agents come in?

**Strands Agents is what we use to build the AI workers.**

Think of it like this:

### Without Strands

You'd have to build a lot of agent plumbing yourself.

### With Strands

You can define:

> "This is my Monitor Agent. Give it these tools. Give it this instruction. Let it reason and use the tools."
> 

Conceptually:

```
                 STRANDS AGENTS
                       │
        ┌──────────────┼──────────────┐
        │              │              │
        ▼              ▼              ▼
   Monitor Agent   Intake Agent   Dispatch Agent
        │              │              │
      Tools          Tools          Tools
        │              │              │
        ▼              ▼              ▼
   River API       WhatsApp       DynamoDB
   Weather API     Forms          Responder DB
```

So **Strands = the framework for creating our agents.**

# 👀 4. Let's understand one agent

Take our **Monitor Agent**.

A normal software program might simply say:

```
IF river > 5m
THEN send alert
```

Our agent can be more flexible.

We give it tools:

```
get_river_level()
get_rainfall()
get_dam_status()
get_previous_reading()
create_incident()
```

And instructions:

> "Monitor the configured river and identify meaningful changes. Do not generate alerts for normal fluctuations. When a safety threshold is crossed, create an incident."
> 

Then Strands lets the agent reason and use those tools.

```
                 MONITOR AGENT
                       │
                       ▼
             "I need river data"
                       │
                       ▼
               get_river_level()
                       │
                       ▼
                    4.6m
                       │
                       ▼
              "Previous was 3.9m"
                       │
                       ▼
             "Rapid increase!"
                       │
                       ▼
               create_incident()
                       │
                       ▼
                 🚨 INCIDENT
```

That's much more interesting than a chatbot.

# ☁️ 5. Now — what is AgentCore?

This is where the terminology can become confusing.

Think of:

### Strands = **the brains/workers we build**

### AgentCore = **the AWS environment/services that help those workers operate**

Very simplified:

```
          🧠 STRANDS AGENT
        "What should I do?"
                │
                ▼
       ☁️ AMAZON BEDROCK
          AGENTCORE
     "Let me run/manage it"
                │
        ┌───────┼────────┐
        ▼       ▼        ▼
     Runtime  Memory   Gateway
        │       │        │
        ▼       ▼        ▼
     Execute  Remember  Access tools
```

So don't think:

> "Strands vs AgentCore"
> 

as competing technologies.

Think:

> **Strands builds the agent. AgentCore helps us deploy and operate the agent.**
> 

# 🏗️ 6. Our complete architecture

Now let's combine everything.

```
                           🌍 REAL WORLD
                                │
             ┌──────────────────┼──────────────────┐
             │                  │                  │
             ▼                  ▼                  ▼
        🌧️ Weather          🌊 River/Dam       👥 Residents
           APIs                Sensors           Messages
             │                  │                  │
             └──────────────────┼──────────────────┘
                                │
                                ▼
                     ☁️ AWS INGESTION LAYER
                                │
                       IoT / Lambda / APIs
                                │
                                ▼
              ┌────────────────────────────────┐
              │       AMAZON BEDROCK            │
              │          AGENTCORE              │
              │                                │
              │   Runtime / Memory / Gateway   │
              │                                │
              │       ┌────────────────┐       │
              │       │ STRANDS AGENTS │       │
              │       └───────┬────────┘       │
              │               │                │
              │    ┌──────────┼───────────┐    │
              │    │          │           │    │
              │    ▼          ▼           ▼    │
              │ Monitor     Intake     Dispatch│
              │ Agent       Agent       Agent   │
              │    │          │           │    │
              │    ▼          ▼           ▼    │
              │ Alert      Requests    Resources│
              │ Agent                   /Match  │
              │                                │
              └───────────────┬────────────────┘
                              │
                              ▼
                       🗄️ SHARED STATE
                         DynamoDB
                              │
               ┌──────────────┼──────────────┐
               ▼              ▼              ▼
            Shelters       Volunteers      Requests
               │              │              │
               └──────────────┼──────────────┘
                              │
                              ▼
                         📱 COMMUNITY
                              │
                              ▼
                    🚑 REAL-WORLD ACTION
```

# 🔄 7. Now let's follow ONE real incident

This is the story I would actually show judges.

## Step 1 — Something changes

Rain becomes very heavy.

```
🌧️ Rainfall
    ↓
🌊 River rising
    ↓
4.1m → 4.5m
```

Monitor Agent detects it.

**Strands Agent** calls:

```
get_river_level()
```

It determines:

> "This is a meaningful change."
> 

Creates an incident.

---

# Step 2 — Alert Agent acts

Monitor hands the incident to Alert Agent.

```
🚨 FLOOD WARNING
        ↓
Affected areas?
        ↓
Area A
Area B
Area C
```

Alert Agent generates the appropriate warning.

Then uses a tool:

```
send_notification()
```

Residents actually receive it.

**This is "real work."**

---

# Step 3 — A resident asks for help

Resident sends:

> "Water has entered my house. We are 5 people. My mother can't walk."
> 

Now Intake Agent gets it.

```
             💬 MESSAGE
                 │
                 ▼
          Intake Agent
                 │
                 ▼
      ┌──────────────────┐
      │ Type: RESCUE     │
      │ People: 5        │
      │ Location: X      │
      │ Medical: YES     │
      │ Urgency: HIGH    │
      └──────────────────┘
```

It creates a real request in DynamoDB.

---

# Step 4 — Dispatch Agent works

Now Dispatch Agent has a job:

> **Find the best available responder.**
> 

It can call:

```
find_nearby_responders()
check_responder_status()
check_vehicle()
assign_responder()
```

Suppose:

```
Responder A
2 km
Boat
Available

Responder B
1 km
No boat
Available

Responder C
5 km
Boat
Busy
```

Agent chooses A.

```
🆘 RESCUE REQUEST
        │
        ▼
   Dispatch Agent
        │
        ▼
🚑 Responder A
        │
        ▼
     ACCEPTED
```

---

# Step 5 — Registry changes

Before:

```
Responder A
AVAILABLE
```

After assignment:

```
Responder A
BUSY
```

And Registry updates it.

Similarly:

```
Shelter A
50 spaces
↓
40 spaces
```

Everyone sees the current state.

---

# Step 6 — Verification

Responder completes rescue.

Clicks:

> **RESCUE COMPLETED**
> 

Now:

```
REQUEST
IN PROGRESS
     ↓
   VERIFY
     ↓
 RESOLVED ✅
```

This is where our **QA / Escalation Agent** comes in.

For critical operations, we can require:

```
AI recommendation
       ↓
Safety check
       ↓
Human approval
       ↓
Action
```

# 🧠 8. So what exactly is AI doing?

This is an important distinction for the hackathon.

We aren't using AI simply to say:

> "There may be a flood."
> 

We're using AI where **unstructured information + reasoning + tool use** is useful.

### AI is useful for:

**Understanding**

> "My mother can't walk and we're trapped."
> 

↓

Structured emergency request.

**Reasoning**

> Which available responder is suitable?
> 

**Communication**

> Convert the warning into Tamil/English and appropriate channel format.
> 

**Coordination**

> Update the request, notify the responder and track status.
> 

**Knowledge retrieval**

> Find the latest verified answer about water safety.
> 

---

# ⚙️ 9. What is NOT AI?

This is equally important.

We should use normal deterministic software for safety-critical rules.

For example:

```
River > 5m
```

should not depend on an LLM's opinion.

Likewise:

```
IF evacuation_required
THEN require human approval
```

should be a system rule.

So our architecture becomes:

```
        DATA
         │
         ▼
   Deterministic
      Rules
         │
         ▼
     AI Agent
    (reasoning)
         │
         ▼
       Tools
         │
         ▼
   Safety Rules
         │
         ▼
 Human approval
    if needed
         │
         ▼
       ACTION
```

**This will make our technical story much stronger.**

# 🏆 10. The one picture I would use in the hackathon

If we need to explain the entire project in **one slide**, I'd use this:
🌧️ REAL WORLD
                         │
            ┌────────────┼────────────┐
            ▼            ▼            ▼
         Weather       River       Residents
           API         Sensor       Messages
            │            │            │
            └────────────┼────────────┘
                         ▼
                 ┌───────────────┐
                 │  STRANDS      │
                 │    AGENTS     │
                 │               │
                 │ 👀 Monitor    │
                 │ 🚨 Alert      │
                 │ 📥 Intake     │
                 │ 🚑 Dispatch   │
                 │ 📚 FAQ        │
                 │ 🛡️ Safety     │
                 └───────┬───────┘
                         │
              Built & deployed with
                         │
                         ▼
              ☁️ AMAZON BEDROCK
                    AGENTCORE
                         │
            ┌────────────┼────────────┐
            ▼            ▼            ▼
         Runtime       Memory       Gateway
            │            │            │
            └────────────┼────────────┘
                         ▼
                   🗄️ DYNAMODB
                 SHARED LIVE STATE
                         │
          ┌──────────────┼──────────────┐
          ▼              ▼              ▼
       Shelters       Volunteers      Requests
          │              │              │
          └──────────────┼──────────────┘
                         ▼
                    🚨 ACTION
                         │
              ┌──────────┼──────────┐
              ▼          ▼          ▼
             📱         🚑         🏠
           Warn       Rescue      Shelter
                         │
                         ▼
                    ✅ VERIFY