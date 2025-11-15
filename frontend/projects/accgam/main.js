import * as THREE from 'https://unpkg.com/three@0.160.1/build/three.module.js';

const { createApp, ref, computed, watch, onMounted, onBeforeUnmount } = Vue;

const indexFromCoord = (x, y, z) => x + y * 3 + z * 9;

const buildWinningLines = () => {
  const lines = [];
  const range = [0, 1, 2];

  // Straight lines along axes
  for (const y of range) {
    for (const z of range) {
      lines.push([indexFromCoord(0, y, z), indexFromCoord(1, y, z), indexFromCoord(2, y, z)]);
    }
  }
  for (const x of range) {
    for (const z of range) {
      lines.push([indexFromCoord(x, 0, z), indexFromCoord(x, 1, z), indexFromCoord(x, 2, z)]);
    }
  }
  for (const x of range) {
    for (const y of range) {
      lines.push([indexFromCoord(x, y, 0), indexFromCoord(x, y, 1), indexFromCoord(x, y, 2)]);
    }
  }

  // Plane diagonals
  for (const z of range) {
    lines.push([indexFromCoord(0, 0, z), indexFromCoord(1, 1, z), indexFromCoord(2, 2, z)]);
    lines.push([indexFromCoord(0, 2, z), indexFromCoord(1, 1, z), indexFromCoord(2, 0, z)]);
  }
  for (const x of range) {
    lines.push([indexFromCoord(x, 0, 0), indexFromCoord(x, 1, 1), indexFromCoord(x, 2, 2)]);
    lines.push([indexFromCoord(x, 2, 0), indexFromCoord(x, 1, 1), indexFromCoord(x, 0, 2)]);
  }
  for (const y of range) {
    lines.push([indexFromCoord(0, y, 0), indexFromCoord(1, y, 1), indexFromCoord(2, y, 2)]);
    lines.push([indexFromCoord(0, y, 2), indexFromCoord(1, y, 1), indexFromCoord(2, y, 0)]);
  }

  // Space diagonals
  lines.push([indexFromCoord(0, 0, 0), indexFromCoord(1, 1, 1), indexFromCoord(2, 2, 2)]);
  lines.push([indexFromCoord(0, 0, 2), indexFromCoord(1, 1, 1), indexFromCoord(2, 2, 0)]);
  lines.push([indexFromCoord(0, 2, 0), indexFromCoord(1, 1, 1), indexFromCoord(2, 0, 2)]);
  lines.push([indexFromCoord(0, 2, 2), indexFromCoord(1, 1, 1), indexFromCoord(2, 0, 0)]);

  return lines;
};

const WINNING_LINES = buildWinningLines();

const detectVictory = (board) => {
  for (const line of WINNING_LINES) {
    const [a, b, c] = line;
    const marker = board[a];
    if (marker && marker === board[b] && marker === board[c]) {
      return { winner: marker, line };
    }
  }
  if (board.every(Boolean)) {
    return { winner: 'draw', line: [] };
  }
  return null;
};

const coordFromIndex = (index) => {
  const z = Math.floor(index / 9);
  const remainder = index % 9;
  const y = Math.floor(remainder / 3);
  const x = remainder % 3;
  return { x, y, z };
};

const ThreeBoard = {
  name: 'ThreeBoard',
  props: {
    board: {
      type: Array,
      required: true,
    },
    winner: {
      type: String,
      default: null,
    },
    winningLine: {
      type: Array,
      default: () => [],
    },
    disabled: {
      type: Boolean,
      default: false,
    },
  },
  emits: ['select'],
  setup(props, { emit }) {
    const mountRef = ref(null);
    let renderer;
    let scene;
    let camera;
    let raycaster;
    let pointer;
    let animationId;
    let cellGroup;
    const cellMeshes = [];
    const baseGeometry = new THREE.BoxGeometry(0.9, 0.9, 0.9);

    const colors = {
      empty: 0x475569,
      x: 0x38bdf8,
      o: 0xfb7185,
      win: 0xf59e0b,
    };

    const initScene = () => {
      if (!mountRef.value) return;
      scene = new THREE.Scene();
      scene.background = new THREE.Color('#010409');

      const { clientWidth, clientHeight } = mountRef.value;
      camera = new THREE.PerspectiveCamera(45, clientWidth / clientHeight, 0.1, 100);
      camera.position.set(6, 6, 10);
      camera.lookAt(0, 0, 0);

      const ambient = new THREE.AmbientLight(0xffffff, 0.85);
      scene.add(ambient);
      const directional = new THREE.DirectionalLight(0xffffff, 0.6);
      directional.position.set(5, 8, 6);
      scene.add(directional);

      cellGroup = new THREE.Group();
      scene.add(cellGroup);
      createCells();

      renderer = new THREE.WebGLRenderer({ antialias: true });
      renderer.setPixelRatio(window.devicePixelRatio);
      renderer.setSize(clientWidth, clientHeight);
      mountRef.value.appendChild(renderer.domElement);

      raycaster = new THREE.Raycaster();
      pointer = new THREE.Vector2();
      renderer.domElement.addEventListener('pointerdown', handlePointerDown);
      window.addEventListener('resize', handleResize);

      animate();
    };

    const createCells = () => {
      const spacing = 1.35;
      for (let z = 0; z < 3; z += 1) {
        for (let y = 0; y < 3; y += 1) {
          for (let x = 0; x < 3; x += 1) {
            const material = new THREE.MeshStandardMaterial({ color: colors.empty });
            const mesh = new THREE.Mesh(baseGeometry, material);
            const index = indexFromCoord(x, y, z);
            mesh.position.set((x - 1) * spacing, (y - 1) * spacing, (z - 1) * spacing);
            mesh.userData.index = index;
            cellMeshes[index] = mesh;
            cellGroup.add(mesh);
          }
        }
      }
      const gridHelper = new THREE.GridHelper(8, 4, 0x475569, 0x1e293b);
      gridHelper.rotation.x = Math.PI / 2;
      gridHelper.position.y = -2.5;
      scene.add(gridHelper);
    };

    const animate = () => {
      animationId = requestAnimationFrame(animate);
      if (cellGroup) {
        cellGroup.rotation.y += 0.0035;
        cellGroup.rotation.x = 0.35 + Math.sin(performance.now() * 0.0005) * 0.1;
      }
      renderer.render(scene, camera);
    };

    const handleResize = () => {
      if (!mountRef.value || !renderer || !camera) return;
      const { clientWidth, clientHeight } = mountRef.value;
      camera.aspect = clientWidth / clientHeight;
      camera.updateProjectionMatrix();
      renderer.setSize(clientWidth, clientHeight);
    };

    const handlePointerDown = (event) => {
      if (props.disabled) return;
      const bounds = renderer.domElement.getBoundingClientRect();
      pointer.x = ((event.clientX - bounds.left) / bounds.width) * 2 - 1;
      pointer.y = -((event.clientY - bounds.top) / bounds.height) * 2 + 1;
      raycaster.setFromCamera(pointer, camera);
      const intersects = raycaster.intersectObjects(cellGroup.children);
      if (intersects.length > 0) {
        const { index } = intersects[0].object.userData;
        emit('select', index);
      }
    };

    const updateCellColors = () => {
      if (!cellMeshes.length) return;
      const winningSet = new Set(props.winningLine || []);
      cellMeshes.forEach((mesh, index) => {
        if (!mesh) return;
        let target = colors.empty;
        const value = props.board[index];
        if (value === 'X') target = colors.x;
        if (value === 'O') target = colors.o;
        if (winningSet.has(index)) target = colors.win;
        mesh.material.color.setHex(target);
        const bump = winningSet.has(index) ? 1.2 : 1;
        mesh.scale.set(bump, bump, bump);
      });
      if (renderer) {
        renderer.domElement.style.cursor = props.disabled ? 'not-allowed' : 'pointer';
      }
    };

    onMounted(() => {
      initScene();
      updateCellColors();
    });

    onBeforeUnmount(() => {
      cancelAnimationFrame(animationId);
      window.removeEventListener('resize', handleResize);
      if (renderer) {
        renderer.domElement.removeEventListener('pointerdown', handlePointerDown);
        renderer.dispose();
      }
    });

    watch(
      () => props.board,
      () => updateCellColors(),
      { deep: true }
    );

    watch(
      () => props.winningLine,
      () => updateCellColors(),
      { deep: true }
    );

    watch(
      () => props.disabled,
      () => updateCellColors()
    );

    return { mountRef };
  },
  template: `<div class="board-stage" ref="mountRef"></div>`,
};

const App = {
  name: 'AccGamApp',
  components: { ThreeBoard },
  setup() {
    const board = ref(Array(27).fill(null));
    const currentPlayer = ref('X');
    const winner = ref(null);
    const winningLine = ref([]);
    const history = ref([]);

    const statusMessage = computed(() => {
      if (winner.value === 'draw') return 'Stalemate detected — the grid is full.';
      if (winner.value === 'X' || winner.value === 'O') return `${winner.value} stabilised three in a row.`;
      return `Commander ${currentPlayer.value}, select a sector.`;
    });

    const disabled = computed(() => Boolean(winner.value));

    const winStatusText = computed(() => {
      if (!winner.value) return 'In play';
      if (winner.value === 'draw') return 'Stalemate';
      return `${winner.value} victory`;
    });

    const recordMove = (index, player) => {
      const { x, y, z } = coordFromIndex(index);
      history.value = [
        { move: history.value.length + 1, player, coord: `${x + 1}-${y + 1}-${z + 1}`, index },
        ...history.value,
      ];
    };

    const handleSelect = (index) => {
      if (board.value[index] || winner.value) return;
      board.value[index] = currentPlayer.value;
      recordMove(index, currentPlayer.value);
      const verdict = detectVictory(board.value);
      if (verdict) {
        winner.value = verdict.winner;
        winningLine.value = verdict.line;
      } else {
        currentPlayer.value = currentPlayer.value === 'X' ? 'O' : 'X';
      }
    };

    const resetBoard = () => {
      board.value = Array(27).fill(null);
      currentPlayer.value = 'X';
      winner.value = null;
      winningLine.value = [];
      history.value = [];
    };

    const undoLastMove = () => {
      if (!history.value.length || winner.value) return;
      const [latest, ...rest] = history.value;
      history.value = rest;
      board.value[latest.index] = null;
      currentPlayer.value = latest.player;
    };

    return {
      board,
      currentPlayer,
      winner,
      winningLine,
      history,
      statusMessage,
      disabled,
      winStatusText,
      handleSelect,
      resetBoard,
      undoLastMove,
    };
  },
  template: `
    <div class="site-wrapper">
      <header class="masthead">
        <div class="hero-copy">
          <p class="eyebrow">Tactical grid module · build 03</p>
          <h1>accgam</h1>
          <p class="lead">Immersive 3D tic-tac-toe built with Vue 3 and Three.js, now docked inside a polished command deck.</p>
          <div class="hero-tags">
            <span class="tag">Three.js</span>
            <span class="tag">Vue 3</span>
            <span class="tag">WebGL</span>
          </div>
        </div>
        <div class="hero-status">
          <div class="stat-card">
            <span>Active commander</span>
            <strong>{{ currentPlayer }}</strong>
            <small>{{ winner ? 'sequence locked' : 'awaiting orders' }}</small>
          </div>
          <p class="status-pill">{{ statusMessage }}</p>
        </div>
      </header>
      <div class="content-grid">
        <section class="panel command-panel">
          <div class="panel-heading">
            <h2>Mission Control</h2>
            <span class="pill live">Live</span>
          </div>
          <p class="intel">
            Deploy three matching markers along any row, column, pillar, plane diagonal, or space diagonal. Each
            selection locks a field; undo is available only before a win state confirms the victor.
          </p>
          <div class="control-buttons">
            <button class="primary" type="button" @click="resetBoard">Deploy New Grid</button>
            <button class="ghost" type="button" @click="undoLastMove" :disabled="!history.length || winner">
              Undo Move
            </button>
          </div>
          <div class="mini-metrics">
            <div>
              <span>Logged moves</span>
              <strong>{{ history.length }}</strong>
            </div>
            <div>
              <span>Win state</span>
              <strong>{{ winStatusText }}</strong>
            </div>
          </div>
        </section>
        <section class="panel board-panel">
          <div class="panel-heading">
            <h2>Holo Grid</h2>
            <span class="pill neutral">3×3×3</span>
          </div>
          <div class="board-shell">
            <three-board
              :board="board"
              :winner="winner"
              :winning-line="winningLine"
              :disabled="disabled"
              @select="handleSelect"
            />
          </div>
        </section>
        <section class="panel history-panel">
          <div class="panel-heading">
            <h2>Telemetry Log</h2>
            <span class="pill neutral">Chrono</span>
          </div>
          <ul class="history-list">
            <li v-if="!history.length">
              <span class="history-move">—</span>
              <div>
                <strong>Awaiting first contact</strong>
                <p>Moves appear here as soon as the grid is engaged.</p>
              </div>
            </li>
            <li v-for="entry in history" :key="entry.move">
              <span class="history-move">#{{ entry.move }}</span>
              <div>
                <strong>{{ entry.player }} marked sector {{ entry.coord }}</strong>
                <p>Index {{ entry.index + 1 }}</p>
              </div>
            </li>
          </ul>
        </section>
      </div>
    </div>
  `,
};

createApp(App).mount('#app');
