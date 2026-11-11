// Vista 3D (three.js): modelo S3DB proposto sobre os pontos LiDAR do edifício.
// Coordenadas da API: x = este, y = norte, z = cima (metros, relativas à origem do
// edifício). Em three.js: X = x, Y = z, Z = −y.
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { CLASS_COLORS } from "./colors.js";

const toThree = (arr) => {
  const out = new Float32Array(arr.length);
  for (let i = 0; i < arr.length; i += 3) {
    out[i] = arr[i];
    out[i + 1] = arr[i + 2];
    out[i + 2] = -arr[i + 1];
  }
  return out;
};

export class Viewer {
  constructor(container) {
    this.container = container;
    this.renderer = new THREE.WebGLRenderer({ antialias: true });
    this.renderer.setPixelRatio(window.devicePixelRatio);
    container.appendChild(this.renderer.domElement);

    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(0x1b1f27);
    this.camera = new THREE.PerspectiveCamera(45, 1, 0.1, 2000);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;

    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x444455, 1.6));
    const sun = new THREE.DirectionalLight(0xffffff, 1.4);
    sun.position.set(30, 60, 20);
    this.scene.add(sun);

    this.groups = { points: new THREE.Group(), context: new THREE.Group(), model: new THREE.Group() };
    Object.values(this.groups).forEach((g) => this.scene.add(g));
    this.pointData = null;
    this.colorMode = "rgb";

    new ResizeObserver(() => this.resize()).observe(container);
    this.resize();
    const loop = () => {
      this.controls.update();
      this.renderer.render(this.scene, this.camera);
      requestAnimationFrame(loop);
    };
    loop();
  }

  resize() {
    const { clientWidth: w, clientHeight: h } = this.container;
    if (!w || !h) return;
    this.renderer.setSize(w, h);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
  }

  static clear(group) {
    for (const obj of [...group.children]) {
      group.remove(obj);
      obj.geometry?.dispose();
      obj.material?.dispose();
    }
  }

  setVisible(name, on) {
    this.groups[name].visible = on;
  }

  setPoints(data) {
    this.pointData = data;
    this.renderPoints();
  }

  setColorMode(mode) {
    this.colorMode = mode;
    this.renderPoints();
  }

  renderPoints() {
    Viewer.clear(this.groups.points);
    Viewer.clear(this.groups.context);
    const d = this.pointData;
    if (!d || !d.count) return;
    const pos = toThree(d.positions);
    const build = (pred) => {
      const idx = [];
      for (let i = 0; i < d.count; i++) if (pred(i)) idx.push(i);
      const p = new Float32Array(idx.length * 3);
      const c = new Float32Array(idx.length * 3);
      idx.forEach((i, j) => {
        p.set(pos.subarray(i * 3, i * 3 + 3), j * 3);
        let rgb;
        if (this.colorMode === "class") rgb = CLASS_COLORS[d.classes[i]] ?? [200, 200, 200];
        else if (this.colorMode === "inside") rgb = d.inside[i] ? [240, 90, 60] : [120, 130, 145];
        else rgb = d.colors.slice(i * 3, i * 3 + 3);
        c.set(rgb.map((v) => v / 255), j * 3);
      });
      const g = new THREE.BufferGeometry();
      g.setAttribute("position", new THREE.BufferAttribute(p, 3));
      g.setAttribute("color", new THREE.BufferAttribute(c, 3));
      return g;
    };
    const mat = (size) => new THREE.PointsMaterial({ size, vertexColors: true, sizeAttenuation: true });
    this.groups.points.add(new THREE.Points(build((i) => d.inside[i]), mat(0.22)));
    this.groups.context.add(new THREE.Points(build((i) => !d.inside[i]), mat(0.16)));
  }

  setTransparent(on) {
    this.transparent = on;
    if (this.lastMesh) this.setModel(this.lastMesh);
  }

  setModel(mesh) {
    // Cores reais que vão ser aplicadas: roof:colour e building:colour finais
    // (ou as cores por omissão dos renderizadores, quando não há tag).
    Viewer.clear(this.groups.model);
    this.lastMesh = mesh;
    if (!mesh) return;
    const transparent = this.transparent ?? true;
    const add = (m, hex, opacity) => {
      if (!m || !m.faces.length) return;
      const g = new THREE.BufferGeometry();
      g.setAttribute("position", new THREE.BufferAttribute(toThree(m.vertices), 3));
      g.setIndex(m.faces);
      g.computeVertexNormals();
      const material = new THREE.MeshStandardMaterial({
        color: new THREE.Color(hex), transparent, opacity: transparent ? opacity : 1,
        side: THREE.DoubleSide, depthWrite: !transparent, roughness: 0.85,
      });
      this.groups.model.add(new THREE.Mesh(g, material));
    };
    // Uma malha por parte (building:part), cada uma com as suas cores.
    const parts = mesh.parts ?? [{ roof: mesh.roof, walls: mesh.walls, colours: mesh.colours }];
    for (const p of parts) {
      add(p.roof, p.colours?.roof ?? "#c8c8c8", 0.6);
      add(p.walls, p.colours?.walls ?? "#e9e4da", 0.35);
      if (parts.length > 1) {
        // Contorno de cada parte, para se ver a divisão.
        for (const ring of p.outline ?? []) {
          const pts = [...ring, ring[0]].map(([x, y]) => new THREE.Vector3(x, 0.08, -y));
          const g = new THREE.BufferGeometry().setFromPoints(pts);
          this.groups.model.add(new THREE.Line(g, new THREE.LineBasicMaterial({ color: 0x60a5fa })));
        }
      }
    }
    // Contorno do footprint ao nível do chão.
    for (const ring of mesh.footprint ?? []) {
      const pts = ring.map(([x, y]) => new THREE.Vector3(x, 0.05, -y));
      const g = new THREE.BufferGeometry().setFromPoints(pts);
      this.groups.model.add(new THREE.Line(g, new THREE.LineBasicMaterial({ color: 0xfacc15 })));
    }
  }

  frame(mesh) {
    // Enquadra o edifício: raio a partir do contorno e da altura do modelo.
    let r = 8;
    let top = 6;
    for (const ring of mesh?.footprint ?? []) for (const [x, y] of ring) r = Math.max(r, Math.hypot(x, y));
    for (const p of mesh?.parts ?? [mesh ?? {}]) {
      const v = p?.roof?.vertices ?? [];
      for (let i = 2; i < v.length; i += 3) top = Math.max(top, v[i]);
    }
    this.controls.target.set(0, top / 2, 0);
    const dist = Math.max(r, top) * 2.6;
    this.camera.position.set(dist * 0.6, dist * 0.55, dist * 0.75);
    this.camera.near = 0.1;
    this.camera.far = dist * 20;
    this.camera.updateProjectionMatrix();
  }

  clearAll() {
    this.pointData = null;
    Object.values(this.groups).forEach((g) => Viewer.clear(g));
  }
}
