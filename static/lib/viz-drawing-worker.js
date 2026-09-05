import {evaluateDrawing} from './viz-drawing.js';
self.onmessage = ({data}) => {
  try { self.postMessage({commands:evaluateDrawing(data.program, data.state)}); }
  catch { self.postMessage({failed:true}); }
};
