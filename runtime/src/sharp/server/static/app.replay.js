/**
 * app.replay.js — 回放系统：帧构建/应用/播放控制/退出
 */
function applyReplayModule(obj) {
  Object.assign(obj, {
    replayProgressLabel() {
      if (!this.replay.active || this.replay.frames.length === 0) return '回放';
      return `回放 ${Math.min(this.replay.frameIndex + 1, this.replay.frames.length)} / ${this.replay.frames.length}`;
    },

    stopReplayTimer() {
      if (!this.replay.timer) return;
      clearTimeout(this.replay.timer);
      this.replay.timer = null;
    },

    updateReplaySpeed() {
      if (!this.replay.active || !this.replay.playing) return;
      this.stopReplayTimer();
      this.scheduleReplayTick();
    },

    replayEventWeight(event) {
      const type = event?.type;
      if (type === 'reason_started') return 1.6;
      if (type === 'intent_declared') return 1.05;
      if (type === 'intent_running') return 0.75;
      if (type === 'intent_concluded' || type === 'project_completed') return 1.6;
      if (type === 'project_created' || type === 'hint_added') return 0.8;
      return 1.0;
    },

    replayEventDurationMs(event) {
      const base = Number(this.replay.stepMs) || 1100;
      return Math.round(base * this.replayEventWeight(event));
    },

    replayTimelineElapsedDurationMs(events, index) {
      let elapsed = 0;
      for (let i = 0; i < index; i += 1) {
        elapsed += this.replayEventDurationMs(events[i]);
      }
      return elapsed;
    },

    scheduleReplayTick() {
      if (!this.replay.active || !this.replay.playing) return;
      if (this.replay.frameIndex >= this.replay.frames.length - 1) {
        this.replay.playing = false;
        return;
      }
      this.stopReplayTimer();
      const currentFrame = this.replay.frames[this.replay.frameIndex];
      const delay = this.replayEventDurationMs(currentFrame?.event);
      this.replay.timer = setTimeout(() => this.advanceProjectReplay(), delay);
    },

    buildInitialReplayProject(sourceProject) {
      const origin = sourceProject.facts.find(fact => fact.id === 'origin');
      const goal = sourceProject.facts.find(fact => fact.id === 'goal');
      return {
        project: {
          ...this.cloneData(sourceProject.project),
          status: 'active',
          reason: null,
        },
        facts: [origin, goal].filter(Boolean).map(fact => this.cloneData(fact)),
        intents: [],
        hints: [],
      };
    },

    buildReplayFrames(sourceProject, baseEvents) {
      const sourceIntents = new Map(sourceProject.intents.map(intent => [intent.id, intent]));
      const replayEvents = [];

      for (const event of baseEvents) {
        if (event.type !== 'intent_declared' || !event.intentId) {
          replayEvents.push(this.cloneData(event));
          continue;
        }

        const sourceIntent = sourceIntents.get(event.intentId);
        replayEvents.push({
          id: `reason-started-${event.intentId}`,
          type: 'reason_started',
          timestamp: sourceIntent?.created_at || event.timestamp,
          actor: sourceIntent?.creator || event.actor || 'reasoner',
          title: sourceIntent?.description || event.title,
          meta: [
            sourceIntent?.id || event.intentId,
            `来源 ${(sourceIntent?.from || event.sourceFactIds || []).join(', ')}`,
          ],
          targetType: 'reason',
          targetId: sourceIntent?.id || event.intentId,
          order: `${event.order}.reason`,
          intentId: sourceIntent?.id || event.intentId,
          producedFactId: null,
          sourceFactIds: [...(sourceIntent?.from || event.sourceFactIds || [])],
        });
        replayEvents.push(this.cloneData(event));
        if (!sourceIntent?.worker) continue;
        replayEvents.push({
          id: `intent-running-${sourceIntent.id}`,
          type: 'intent_running',
          timestamp: sourceIntent.last_heartbeat_at || sourceIntent.created_at,
          actor: sourceIntent.worker,
          title: sourceIntent.description,
          meta: [sourceIntent.id, `执行者 ${sourceIntent.worker}`],
          targetType: 'intent',
          targetId: sourceIntent.id,
          order: `${event.order}.run`,
          intentId: sourceIntent.id,
          producedFactId: null,
          sourceFactIds: [...sourceIntent.from],
        });
      }

      const replayProject = this.buildInitialReplayProject(sourceProject);
      const frames = [];
      for (const event of replayEvents) {
        this.applyReplayEvent(replayProject, sourceProject, event);
        frames.push({
          event: this.cloneData(event),
          project: this.cloneData(replayProject),
        });
      }

      if (frames.length > 0) {
        frames[frames.length - 1].project.project.status = sourceProject.project.status;
        frames[frames.length - 1].project.project.reason = this.cloneData(sourceProject.project.reason);
      }
      return frames;
    },

    applyReplayEvent(replayProject, sourceProject, event) {
      if (!replayProject || !sourceProject || !event) return;
      const sourceIntent = event.intentId
        ? sourceProject.intents.find(intent => intent.id === event.intentId) || null
        : null;

      if (event.type === 'project_created') {
        replayProject.project.title = sourceProject.project.title;
        replayProject.project.status = 'active';
        return;
      }

      if (event.type === 'hint_added') {
        const hint = sourceProject.hints.find(item => item.id === event.targetId);
        if (hint && !replayProject.hints.some(item => item.id === hint.id)) {
          replayProject.hints.push(this.cloneData(hint));
        }
        return;
      }

      if (event.type === 'reason_started') {
        replayProject.project.reason = {
          worker: event.actor || 'reasoner',
          trigger: 'new_facts',
          started_at: event.timestamp,
          last_heartbeat_at: event.timestamp,
        };
        return;
      }

      if (event.type === 'intent_declared') {
        if (!sourceIntent) return;
        replayProject.project.reason = null;
        if (!replayProject.intents.some(intent => intent.id === sourceIntent.id)) {
          replayProject.intents.push({
            id: sourceIntent.id,
            from: [...sourceIntent.from],
            to: null,
            description: sourceIntent.description,
            creator: sourceIntent.creator,
            worker: null,
            last_heartbeat_at: null,
            created_at: sourceIntent.created_at,
            concluded_at: null,
          });
        }
        return;
      }

      if (event.type === 'intent_running') {
        if (!sourceIntent) return;
        const replayIntent = replayProject.intents.find(intent => intent.id === sourceIntent.id);
        if (!replayIntent) return;
        replayIntent.worker = sourceIntent.worker || sourceIntent.creator;
        replayIntent.last_heartbeat_at = sourceIntent.last_heartbeat_at || sourceIntent.created_at;
        return;
      }

      if (event.type !== 'intent_concluded' && event.type !== 'project_completed') return;
      if (!sourceIntent) return;

      const replayIntent = replayProject.intents.find(intent => intent.id === sourceIntent.id);
      if (replayIntent) {
        replayIntent.to = sourceIntent.to;
        replayIntent.worker = sourceIntent.worker || sourceIntent.creator;
        replayIntent.last_heartbeat_at = sourceIntent.last_heartbeat_at;
        replayIntent.concluded_at = sourceIntent.concluded_at;
      }

      if (event.type === 'project_completed') {
        replayProject.project.status = 'completed';
        return;
      }

      const producedFact = sourceProject.facts.find(fact => fact.id === sourceIntent.to);
      if (producedFact && !replayProject.facts.some(fact => fact.id === producedFact.id)) {
        replayProject.facts.push(this.cloneData(producedFact));
      }
    },

    applyReplayFrame(frameIndex, options = {}) {
      if (!this.replay.active) return;
      const { reinitialize = false } = options;
      const frame = this.replay.frames[frameIndex];
      if (!frame) return;

      this.replay.frameIndex = frameIndex;
      this.project = this.cloneData(frame.project);
      this.replay.visibleEvents = this.replay.frames
        .slice(0, frameIndex + 1)
        .map(item => this.cloneData(item.event))
        .filter(entry => entry.type !== 'reason_started')
        .map((entry, index, list) => ({ ...entry, isLast: index === list.length - 1 }));
      this._timelineEventsCacheProject = null;
      this._timelineEventsCache = [];

      if (reinitialize) {
        this.teardownAutoFit();
        if (this.cy) {
          this.cy.destroy();
          this.cy = null;
        }
        this.$nextTick(() => {
          this.initGraph();
          this.followReplayTimelineTail();
        });
        return;
      }

      this.updateGraph();
      this.followReplayTimelineTail();
    },

    async startProjectReplay() {
      if (!this.project || this.replay.active || !this.selectedProjectId) return;
      try {
        const sourceProject = await this.api('GET', `/projects/${this.selectedProjectId}`);
        const baseEvents = this.timelineEvents().map(event => ({
          ...event,
          meta: [...(event.meta || [])],
          sourceFactIds: [...(event.sourceFactIds || [])],
        }));
        const frames = this.buildReplayFrames(sourceProject, baseEvents);
        if (frames.length === 0) {
          this.showToast('没有可回放的时间线', 'error');
          return;
        }

        const stepMs = String(this.replay.stepMs || '1100');
        this.stopReplayTimer();
        this.replay = {
          active: true,
          playing: true,
          stepMs,
          frameIndex: -1,
          frames,
          visibleEvents: [],
          sourceProject,
          timer: null,
        };
        this.polling = false;
        this.sideTab = 'log';
        this.selectedNode = null;
        this.selectedFacts = [];
        this.selectedTimelineEntryId = null;
        this.applyReplayFrame(0, { reinitialize: true });
        this.scheduleReplayTick();
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    advanceProjectReplay() {
      if (!this.replay.active) return;
      if (this.replay.frameIndex >= this.replay.frames.length - 1) {
        this.replay.playing = false;
        this.stopReplayTimer();
        return;
      }
      this.applyReplayFrame(this.replay.frameIndex + 1);
      this.scheduleReplayTick();
    },

    toggleProjectReplayPlayback() {
      if (!this.replay.active) return;
      if (this.replay.playing) {
        this.replay.playing = false;
        this.stopReplayTimer();
        return;
      }
      if (this.replay.frameIndex >= this.replay.frames.length - 1) {
        this.restartProjectReplay();
        return;
      }
      this.replay.playing = true;
      this.scheduleReplayTick();
    },

    restartProjectReplay() {
      if (!this.replay.active || this.replay.frames.length === 0) return;
      this.stopReplayTimer();
      this.replay.playing = true;
      this.selectedNode = null;
      this.selectedFacts = [];
      this.selectedTimelineEntryId = null;
      this.applyReplayFrame(0, { reinitialize: true });
      this.scheduleReplayTick();
    },

    async exitProjectReplay() {
      if (!this.replay.active) return;
      const projectId = this.selectedProjectId;
      const stepMs = String(this.replay.stepMs || '1100');
      this.stopReplayTimer();
      this.replay = {
        active: false,
        playing: false,
        stepMs,
        frameIndex: -1,
        frames: [],
        visibleEvents: [],
        sourceProject: null,
        timer: null,
      };
      this._timelineEventsCacheProject = null;
      this._timelineEventsCache = [];
      this.polling = true;
      if (!projectId) return;
      await this.loadProject(projectId);
      this.$nextTick(() => {
        this.teardownAutoFit();
        if (this.cy) {
          this.cy.destroy();
          this.cy = null;
        }
        this.initGraph();
      });
    },
  });
}
