"use strict";

const state = {
  metadata: null,
  valueRange: [0, 1],
  yaw: 0,
  pitch: -0.65,
  distance: 3.2,
  pointSize: 3,
  points: [],
  gl: null,
  program: null,
  buffer: null,
  ranges: { voxels: 0, nodes: 0, lines: 0 },
  pointGeneration: 0,
  sliceGeneration: {},
  product: "motion",
  pointBounds: null,
};

const $ = (selector) => document.querySelector(selector);
const status = $("#status");

function palette(t) {
  const stops = [
    [0.00, [7, 16, 20]], [0.18, [19, 48, 61]], [0.43, [36, 105, 128]],
    [0.68, [83, 222, 207]], [0.86, [255, 189, 90]], [1.00, [255, 243, 191]],
  ];
  const x = Math.max(0, Math.min(1, t));
  for (let i = 1; i < stops.length; i++) {
    if (x <= stops[i][0]) {
      const [a, ca] = stops[i - 1];
      const [b, cb] = stops[i];
      const f = (x - a) / (b - a);
      return ca.map((v, j) => Math.round(v + (cb[j] - v) * f));
    }
  }
  return stops.at(-1)[1];
}

function normalized(value) {
  const [low, high] = state.valueRange;
  if (high <= low) return 0;
  return Math.sqrt(Math.max(0, Math.min(1, (value - low) / (high - low))));
}

async function request(path) {
  const response = await fetch(path);
  if (!response.ok) throw new Error((await response.json()).error || response.statusText);
  return response.json();
}

function axisCoordinate(axis, index) {
  const axisIndex = { x: 2, y: 1, z: 0 }[axis];
  const count = state.metadata.shape[axisIndex];
  const [minimum, maximum] = state.metadata.bounds_m[axis];
  return count === 1 ? minimum : minimum + index * (maximum - minimum) / (count - 1);
}

function drawSlice(canvas, payload) {
  const rows = payload.values.length;
  const columns = payload.values[0]?.length || 0;
  const dpr = window.devicePixelRatio || 1;
  const width = Math.max(1, Math.round(canvas.clientWidth * dpr));
  const height = Math.max(1, Math.round(canvas.clientHeight * dpr));
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }
  const offscreen = document.createElement("canvas");
  offscreen.width = columns;
  offscreen.height = rows;
  const context = offscreen.getContext("2d");
  const image = context.createImageData(columns, rows);
  for (let row = 0; row < rows; row++) {
    for (let column = 0; column < columns; column++) {
      const sourceRow = rows - 1 - row;
      const confidence = payload.confidence[sourceRow][column];
      const color = confidence > 0 ? palette(normalized(payload.values[sourceRow][column])) : [5, 10, 12];
      const at = (row * columns + column) * 4;
      image.data.set([...color, 255], at);
    }
  }
  context.putImageData(image, 0, 0);
  const target = canvas.getContext("2d");
  target.imageSmoothingEnabled = false;
  target.clearRect(0, 0, width, height);
  target.drawImage(offscreen, 0, 0, width, height);
  target.strokeStyle = "rgba(98,231,224,.24)";
  target.lineWidth = dpr;
  target.strokeRect(0.5 * dpr, 0.5 * dpr, width - dpr, height - dpr);
}

async function updateSlice(article) {
  const plane = article.dataset.plane;
  const slider = article.querySelector("input");
  const generation = (state.sliceGeneration[plane] || 0) + 1;
  state.sliceGeneration[plane] = generation;
  const index = slider.value;
  const payload = await request(`/api/v1/slices/${plane}?index=${index}&product=${state.product}`);
  if (state.sliceGeneration[plane] !== generation) return;
  article.querySelector("output").textContent = `${payload.fixed_axis.toUpperCase()} ${payload.coordinate_m.toFixed(2)} m`;
  drawSlice(article.querySelector("canvas"), payload);
}

function shader(gl, type, source) {
  const item = gl.createShader(type);
  gl.shaderSource(item, source);
  gl.compileShader(item);
  if (!gl.getShaderParameter(item, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(item));
  return item;
}

function initWebGL() {
  const canvas = $("#scene");
  const gl = canvas.getContext("webgl", { antialias: true, alpha: false });
  if (!gl) throw new Error("WebGL is unavailable in this browser");
  const vertex = shader(gl, gl.VERTEX_SHADER, `
    attribute vec3 aPosition;
    attribute float aIntensity;
    attribute float aKind;
    uniform float uYaw;
    uniform float uPitch;
    uniform float uDistance;
    uniform float uAspect;
    uniform float uPointSize;
    varying float vIntensity;
    varying float vKind;
    void main() {
      float cy = cos(uYaw), sy = sin(uYaw), cp = cos(uPitch), sp = sin(uPitch);
      vec3 y = vec3(cy*aPosition.x + sy*aPosition.z, aPosition.y, -sy*aPosition.x + cy*aPosition.z);
      vec3 p = vec3(y.x, cp*y.y - sp*y.z, sp*y.y + cp*y.z);
      p.z -= uDistance;
      float near = 0.1, far = 20.0, f = 1.75;
      gl_Position = vec4(p.x*f/uAspect, p.y*f, ((far+near)/(near-far))*p.z + (2.0*far*near)/(near-far), -p.z);
      gl_PointSize = aKind > 0.5 ? 15.0 : uPointSize * (1.0 + 1.8*aIntensity);
      vIntensity = aIntensity;
      vKind = aKind;
    }
  `);
  const fragment = shader(gl, gl.FRAGMENT_SHADER, `
    precision mediump float;
    varying float vIntensity;
    varying float vKind;
    vec3 color(float t) {
      vec3 a = vec3(0.075,0.25,0.32), b = vec3(0.32,0.9,0.84), c = vec3(1.0,0.74,0.35);
      return t < 0.65 ? mix(a,b,t/0.65) : mix(b,c,(t-0.65)/0.35);
    }
    void main() {
      if (vKind < 0.5) {
        vec2 d = gl_PointCoord - vec2(0.5);
        if (dot(d,d) > 0.25) discard;
        gl_FragColor = vec4(color(vIntensity), 1.0);
      } else if (vKind < 1.5) {
        vec2 d = gl_PointCoord - vec2(0.5);
        if (dot(d,d) > 0.25) discard;
        gl_FragColor = vec4(0.38,0.91,0.88,1.0);
      } else {
        gl_FragColor = vec4(0.28,0.39,0.43,0.7);
      }
    }
  `);
  const program = gl.createProgram();
  gl.attachShader(program, vertex); gl.attachShader(program, fragment); gl.linkProgram(program);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(program));
  state.gl = gl; state.program = program; state.buffer = gl.createBuffer();
  gl.useProgram(program);
  gl.enable(gl.DEPTH_TEST);

  const pointers = new Map();
  let pinchDistance = null;
  canvas.addEventListener("pointerdown", (event) => {
    pointers.set(event.pointerId, [event.clientX, event.clientY]);
    canvas.setPointerCapture(event.pointerId);
  });
  canvas.addEventListener("pointermove", (event) => {
    const prior = pointers.get(event.pointerId);
    if (!prior) return;
    pointers.set(event.pointerId, [event.clientX, event.clientY]);
    if (pointers.size === 1) {
      state.yaw += (event.clientX - prior[0]) * 0.009;
    state.pitch = Math.max(-Math.PI/2, Math.min(Math.PI/2, state.pitch + (event.clientY - prior[1]) * 0.009));
    } else if (pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      const distance = Math.hypot(a[0]-b[0], a[1]-b[1]);
      if (pinchDistance) state.distance = Math.max(1.8, Math.min(7, state.distance * pinchDistance/distance));
      pinchDistance = distance;
    }
    render3D();
  });
  const release = (event) => { pointers.delete(event.pointerId); pinchDistance = null; };
  canvas.addEventListener("pointerup", release);
  canvas.addEventListener("pointercancel", release);
  canvas.addEventListener("wheel", (event) => {
    event.preventDefault();
    state.distance = Math.max(1.8, Math.min(7, state.distance + event.deltaY * 0.002));
    render3D();
  }, { passive: false });
}

function normalizedPosition(point) {
  const bounds = state.metadata.bounds_m;
  const spans = [bounds.x[1]-bounds.x[0], bounds.y[1]-bounds.y[0], bounds.z[1]-bounds.z[0]];
  const scale = Math.max(...spans) || 1;
  return [
    (point[0] - (bounds.x[0]+bounds.x[1])/2) * 2/scale,
    (point[2] - (bounds.z[0]+bounds.z[1])/2) * 2/scale,
    (point[1] - (bounds.y[0]+bounds.y[1])/2) * 2/scale,
  ];
}

function rebuildGeometry() {
  const vertices = [];
  const axisIndexes = { x: 0, y: 1, z: 2 };
  const visiblePoints = state.pointBounds ? state.points.filter((point) =>
    Object.entries(axisIndexes).every(([axis, index]) =>
      point[index] >= state.pointBounds[axis][0] && point[index] <= state.pointBounds[axis][1]
    )
  ) : state.points;
  for (const point of visiblePoints) vertices.push(...normalizedPosition(point), normalized(point[3]), 0);
  state.ranges.voxels = visiblePoints.length;
  $("#point-count").textContent = visiblePoints.length.toLocaleString();
  const nodes = state.metadata.geometry_nodes || [];
  for (const node of nodes) vertices.push(...normalizedPosition(node.position_m), 1, 1);
  state.ranges.nodes = nodes.length;
  const b = state.metadata.bounds_m;
  const corners = [
    [b.x[0],b.y[0],b.z[0]], [b.x[1],b.y[0],b.z[0]], [b.x[1],b.y[1],b.z[0]], [b.x[0],b.y[1],b.z[0]],
    [b.x[0],b.y[0],b.z[1]], [b.x[1],b.y[0],b.z[1]], [b.x[1],b.y[1],b.z[1]], [b.x[0],b.y[1],b.z[1]],
  ].map(normalizedPosition);
  const edges = [[0,1],[1,2],[2,3],[3,0],[4,5],[5,6],[6,7],[7,4],[0,4],[1,5],[2,6],[3,7]];
  for (const [a, c] of edges) vertices.push(...corners[a], 0, 2, ...corners[c], 0, 2);
  state.ranges.lines = edges.length * 2;
  const gl = state.gl;
  gl.bindBuffer(gl.ARRAY_BUFFER, state.buffer);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(vertices), gl.STATIC_DRAW);
  const stride = 5 * Float32Array.BYTES_PER_ELEMENT;
  for (const [name, size, offset] of [["aPosition",3,0],["aIntensity",1,3],["aKind",1,4]]) {
    const location = gl.getAttribLocation(state.program, name);
    gl.enableVertexAttribArray(location);
    gl.vertexAttribPointer(location, size, gl.FLOAT, false, stride, offset * 4);
  }
  render3D();
}

function render3D() {
  const gl = state.gl;
  if (!gl) return;
  const canvas = gl.canvas, dpr = window.devicePixelRatio || 1;
  const width = Math.max(1, Math.round(canvas.clientWidth*dpr)), height = Math.max(1, Math.round(canvas.clientHeight*dpr));
  if (canvas.width !== width || canvas.height !== height) { canvas.width = width; canvas.height = height; }
  gl.viewport(0,0,width,height); gl.clearColor(0.025,0.055,0.068,1); gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT);
  gl.useProgram(state.program);
  for (const [name, value] of [["uYaw",state.yaw],["uPitch",state.pitch],["uDistance",state.distance],["uAspect",width/height],["uPointSize",state.pointSize]]) {
    gl.uniform1f(gl.getUniformLocation(state.program,name),value);
  }
  gl.bindBuffer(gl.ARRAY_BUFFER,state.buffer);
  gl.drawArrays(gl.POINTS,0,state.ranges.voxels);
  gl.drawArrays(gl.LINES,state.ranges.voxels+state.ranges.nodes,state.ranges.lines);
  gl.disable(gl.DEPTH_TEST);
  gl.drawArrays(gl.POINTS,state.ranges.voxels,state.ranges.nodes);
  gl.enable(gl.DEPTH_TEST);
  positionNodeLabels(width/dpr, height/dpr);
}

function positionNodeLabels(width, height) {
  const labels = $("#node-labels").children;
  const nodes = state.metadata.geometry_nodes || [];
  const cy=Math.cos(state.yaw), sy=Math.sin(state.yaw), cp=Math.cos(state.pitch), sp=Math.sin(state.pitch);
  const aspect = width/height, f=1.75;
  nodes.forEach((node,index) => {
    const source = normalizedPosition(node.position_m);
    const x = cy*source[0] + sy*source[2];
    const y = source[1];
    const z = -sy*source[0] + cy*source[2];
    const py = cp*y - sp*z;
    const pz = sp*y + cp*z - state.distance;
    const visible = -pz > 0.1;
    labels[index].hidden = !visible;
    if (!visible) return;
    const ndcX = (x*f/aspect)/(-pz), ndcY = (py*f)/(-pz);
    labels[index].style.left = `${(ndcX*0.5+0.5)*width}px`;
    labels[index].style.top = `${(1-(ndcY*0.5+0.5))*height}px`;
  });
}

let pointTimer;
async function updatePoints() {
  const generation = ++state.pointGeneration;
  const percentile = Number($("#percentile").value);
  $("#percentile-value").value = `${percentile.toFixed(1)}%`;
  const payload = await request(`/api/v1/points?percentile=${percentile}&limit=50000&product=${state.product}`);
  if (state.pointGeneration !== generation) return;
  state.points = payload.points;
  state.valueRange = payload.value_range;
  $("#peak-value").textContent = payload.value_range[1].toExponential(2);
  rebuildGeometry();
  await Promise.all([...document.querySelectorAll(".slice")].map(updateSlice));
}

function configure(metadata) {
  state.metadata = metadata;
  state.pointBounds = {};
  for (const axis of ["x", "y", "z"]) {
    const [minimum, maximum] = metadata.bounds_m[axis];
    state.pointBounds[axis] = [minimum, maximum];
    for (const edge of ["min", "max"]) {
      const input = $(`#${axis}-${edge}`);
      input.min = minimum;
      input.max = maximum;
      input.step = metadata.spacing_m;
      input.value = edge === "min" ? minimum : maximum;
      $(`#${axis}-${edge}-value`).value = `${Number(input.value).toFixed(2)} m`;
      input.addEventListener("input", () => {
        const minimumInput = $(`#${axis}-min`);
        const maximumInput = $(`#${axis}-max`);
        if (Number(minimumInput.value) > Number(maximumInput.value)) input.value = edge === "min" ? maximumInput.value : minimumInput.value;
        state.pointBounds[axis] = [Number(minimumInput.value), Number(maximumInput.value)];
        $(`#${axis}-min-value`).value = `${state.pointBounds[axis][0].toFixed(2)} m`;
        $(`#${axis}-max-value`).value = `${state.pointBounds[axis][1].toFixed(2)} m`;
        if (state.gl) rebuildGeometry();
      });
    }
  }
  $("#grid-shape").textContent = metadata.shape.join(" × ");
  $("#spacing").textContent = `${metadata.spacing_m.toFixed(3)} m`;
  $("#links").textContent = metadata.directed_links.length;
  $("#observations").textContent = (metadata.processing?.quality?.accepted_observations || 0).toLocaleString();
  $("#geometry-revision").textContent = metadata.geometry_revision || "UNVERSIONED";
  const placeholder = !metadata.geometry_revision || metadata.geometry_revision.includes("replace-with");
  const calibration = metadata.geometry_provenance?.calibration_status;
  const rangeDerived = calibration && calibration !== "surveyed-calibrated";
  const unsafe = placeholder || rangeDerived;
  $("#geometry-warning").hidden = !unsafe;
  if (placeholder) {
    $("#geometry-warning-title").textContent = "PLACEHOLDER GEOMETRY";
    $("#geometry-warning-text").textContent = "These antenna coordinates are examples and have no physical validity.";
  } else if (rangeDerived) {
    $("#geometry-warning-title").textContent = "LIVE RANGE-DERIVED GEOMETRY";
    $("#geometry-warning-text").textContent = "Coordinates fit current UWB ranges; antenna-delay calibration has not been independently surveyed.";
  }
  const labels = $("#node-labels");
  const register = $("#antenna-list");
  for (const node of metadata.geometry_nodes || []) {
    const label = document.createElement("span");
    label.className = "node-label";
    label.textContent = `N${node.node_id}`;
    labels.appendChild(label);
    const row = document.createElement("div");
    row.className = "antenna-row";
    row.innerHTML = `<strong>N${node.node_id}</strong><span>${node.position_m.map(value => value.toFixed(2)).join(" / ")} m</span>`;
    register.appendChild(row);
  }
  const fixedAxes = { xy: ["z",0], xz: ["y",1], yz: ["x",2] };
  for (const article of document.querySelectorAll(".slice")) {
    const [axis, shapeIndex] = fixedAxes[article.dataset.plane];
    const slider = article.querySelector("input");
    slider.max = metadata.shape[shapeIndex]-1;
    slider.value = Math.floor((metadata.shape[shapeIndex]-1)/2);
    slider.addEventListener("input", () => updateSlice(article).catch(showError));
    article.querySelector("output").textContent = `${axis.toUpperCase()} ${axisCoordinate(axis,slider.value).toFixed(2)} m`;
  }
  if (!metadata.products?.static) $("#product-static").hidden = true;
}

function showError(error) {
  console.error(error);
  status.textContent = error.message.toUpperCase();
  status.parentElement.classList.add("error");
}

async function start() {
  try {
    configure(await request("/api/v1/metadata"));
    initWebGL();
    await updatePoints();
    status.textContent = "REPLAY READY";
    $("#percentile").addEventListener("input", () => {
      clearTimeout(pointTimer);
      $("#percentile-value").value = `${Number($("#percentile").value).toFixed(1)}%`;
      pointTimer = setTimeout(() => updatePoints().catch(showError), 120);
    });
    $("#point-size").addEventListener("input", (event) => {
      state.pointSize = Number(event.target.value);
      $("#point-size-value").value = state.pointSize.toFixed(1);
      render3D();
    });
    const selectProduct = async (product) => {
      state.product = product;
      $("#product-motion").classList.toggle("active", product === "motion");
      $("#product-static").classList.toggle("active", product === "static");
      $("#product-title").textContent = product === "static" ? "Static Environment Field" : "Motion Residual Field";
      await updatePoints();
    };
    $("#product-motion").addEventListener("click", () => selectProduct("motion").catch(showError));
    $("#product-static").addEventListener("click", () => selectProduct("static").catch(showError));
    $("#top-view").addEventListener("click", () => { state.yaw=0; state.pitch=-Math.PI/2; state.distance=3.2; render3D(); });
    $("#reset-view").addEventListener("click", () => { state.yaw=0; state.pitch=-0.65; state.distance=3.2; render3D(); });
    window.addEventListener("resize", () => { render3D(); document.querySelectorAll(".slice").forEach((item) => updateSlice(item).catch(showError)); });
  } catch (error) { showError(error); }
}

start();
